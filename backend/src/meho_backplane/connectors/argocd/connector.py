# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""ArgoCdConnector — hand-rolled HttpConnector subclass for ArgoCD 3.x.

Skeleton-only — bearer-token auth + fingerprint + probe + the G0.6
dispatch shim. The curated read ops (``argocd.app.list`` /
``argocd.app.get`` / ``argocd.app.diff`` / ``argocd.app.resource_tree`` /
``argocd.appproject.list`` / ``argocd.repo.list``) arrive in G3.12-T2 via
``register_typed_operation``.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.argocd.__init__` (versioned + wildcard,
per G0.15-T6 dual registration).

Auth
----

``argocd-server`` authenticates with a JWT **bearer token** sent on every
request: ``Authorization: Bearer <token>``. The token is an ArgoCD
project/account API token minted in ArgoCD (``argocd account
generate-token`` or a ``project`` token) and stored under the target's
``secret_ref`` as a KV-v2 secret with a ``token`` field. The connector
caches the loaded token (read once from Vault via an injectable loader)
and computes the ``Authorization: Bearer`` header on each
:meth:`auth_headers` call.

This is simpler than the SDDC Manager / vmware session-POST (no login
round-trip, no session cookie) and the GitHub App-JWT exchange (no
short-lived-token mint): the stored token is sent verbatim. There is no
username component — unlike Harbor's Basic auth, the credential is a
single opaque string.

Auth model gating
-----------------

The bearer token is a shared service-account credential, so this
connector locks to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or ``None``
for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested
mode — the same boundary the Harbor and SDDC Manager connectors enforce.

Fingerprint
-----------

``GET /api/version`` returns ArgoCD's ``VersionMessage`` payload (an
unauthenticated endpoint on ``argocd-server``). The connector surfaces
``Version`` as the canonical ``version`` and carries the build-tool
versions (``BuildDate``, ``KustomizeVersion``, ``HelmVersion``,
``KubectlVersion``) under ``extras`` — the same payload an operator gets
from ``argocd version -o json`` (server block). Field names are the
gRPC-gateway-serialized proto field names (PascalCase) from
``server/version/version.proto``.

Probe
-----

``probe()`` delegates to ``fingerprint()`` — the same precedent the SDDC
Manager and NSX connectors established. ``GET /api/version`` is a cheap,
unauthenticated reachability check; ArgoCD exposes no dedicated composite
health endpoint comparable to Harbor's ``/api/v2.0/health``, so the
version probe is the right reachability surface.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim :meth:`execute`
exists for ABC compatibility but the curated read ops land via
G3.12-T2's ``register_typed_operation`` upserts. Until then, the
connector is registered and discoverable but ``execute(target, op_id,
...)`` against any ``op_id`` resolves to "unknown operation" at the
dispatcher layer — the correct behaviour for a registered-but-empty
connector at this Task's stage.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import is_system_operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.argocd.session import (
    ARGOCD_TOKEN_FIELD,
    ArgoCdCredentialsLoader,
    ArgoCdTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["ArgoCdConnector"]

_log = structlog.get_logger(__name__)

#: The ArgoCD server version endpoint. Unauthenticated; returns the
#: ``VersionMessage`` payload. Used by both fingerprint() and probe().
_VERSION_PATH = "/api/version"
_PROBE_METHOD = f"GET {_VERSION_PATH}"


def _version_retryable(exc: BaseException) -> bool:
    """Retry the version probe on connection errors and 5xx; never on 4xx.

    Mirrors :func:`HttpConnector._retryable`'s policy. Defined locally so
    the unauthenticated version GET (which bypasses the base
    ``_request_json`` retry wrapper because it must not send an
    ``Authorization`` header) keeps the same idempotent-GET retry
    semantics without reaching into the adapter module's private name.
    """
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3 targets).
    Any other value is rejected by the caller. Same predicate the Harbor /
    SDDC Manager / NSX precedents use; inlined here to keep connectors
    decoupled.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


class ArgoCdConnector(HttpConnector):
    """ArgoCD 3.x REST connector with bearer-token auth.

    Per-target token cached in :attr:`_creds_cache` (loaded once via the
    injectable :class:`ArgoCdCredentialsLoader`). The bearer token is sent
    on every request via ``Authorization: Bearer <token>`` — no session is
    established and no 401-driven re-login is needed.

    The :attr:`priority` is set to ``1`` so a future ``GenericRestConnector``
    auto-shim that somehow registers for the same triple loses the
    registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"argocd-api-3.x"`` -> (``"argocd"``, ``"3.x"``, ``"argocd-api"``).
    product = "argocd"
    version = "3.x"
    impl_id = "argocd-api"
    supported_version_range = ">=2.0,<4.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: ArgoCdCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[str, dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: ArgoCdCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: ArgoCdTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <token>"}`` for the request.

        Loads the API token from Vault on first call against *target*,
        caches it, and reuses the cached value on subsequent calls. The
        full ``operator`` is forwarded to :meth:`_load_credentials` so the
        live default loader reads the per-target Vault secret under the
        operator's identity (``vault_client_for_operator(operator)``).
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` selects the Vault-sourced
        token once the loader has resolved it; the operator's JWT only
        authenticates the read, not the ArgoCD request itself.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"ArgoCdConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target, operator)
        return {"Authorization": f"Bearer {creds[ARGOCD_TOKEN_FIELD]}"}

    async def _load_credentials(
        self, target: ArgoCdTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return the cached token for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same
        target; subsequent calls take the fast path under the same lock.
        The loaded dict must contain a ``"token"`` key; a missing key
        raises a :exc:`RuntimeError` naming the target and the missing key
        so operators can identify a misconfigured Vault path.

        ``operator`` is forwarded to the :class:`ArgoCdCredentialsLoader`
        so the default loader can read the per-target Vault secret under
        the operator's identity (G3.10-T1's live read). The default loader
        is the thin argocd-specific entry point to the shared
        operator-context Vault read; injected test loaders accept the same
        ``(target, operator)`` pair.

        The cache fast-path is closed to the synthesised system operator
        (``is_system_operator``): a system/operator-less caller always runs
        the loader so its fail-closed guard applies, and can never be
        served a warm token a real operator primed but it could not resolve
        itself (#1008). Real-operator behaviour is unchanged — cold load →
        cache → reuse.
        """
        async with self._creds_lock:
            cached = self._creds_cache.get(target.name)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            if ARGOCD_TOKEN_FIELD not in raw:
                raise RuntimeError(
                    f"argocd credentials loader for target {target.name!r} returned a "
                    f"dict missing required key {ARGOCD_TOKEN_FIELD!r}; need "
                    "{'token': str}"
                )
            self._creds_cache[target.name] = raw
            _log.info(
                "argocd_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def _get_version_unauthenticated(self, target: ArgoCdTargetLike) -> dict[str, Any]:
        """Retried ``GET /api/version`` with **no** ``Authorization`` header.

        ``argocd-server`` serves ``/api/version`` unauthenticated, so the
        fingerprint/probe path must not require a resolvable bearer token —
        it has to work on a freshly-registered target before its Vault
        secret is configured. The base :meth:`HttpConnector._get_json`
        always calls :meth:`auth_headers` (and thus the credential loader),
        so this helper hits the pooled client directly. Retry semantics
        match the base class: idempotent GET, 3 retries on connection
        errors / 5xx with exponential backoff.
        """

        @retry(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
            retry=retry_if_exception(_version_retryable),
            reraise=True,
        )
        async def _do_get() -> dict[str, Any]:
            client = await self._http_client(target)
            resp = await client.get(_VERSION_PATH)
            resp.raise_for_status()
            return resp.json()  # type: ignore[no-any-return]

        return await _do_get()

    async def fingerprint(
        self,
        target: ArgoCdTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /api/version``.

        ArgoCD's ``VersionMessage`` carries ``Version`` (the server
        version, e.g. ``"v3.3.9+abc1234"``) plus the bundled build-tool
        versions. ``Version`` becomes the canonical ``version``; the
        build-tool fields land under ``extras`` so an operator gets the
        same view ``argocd version -o json`` exposes for the server block.

        The ``/api/version`` endpoint is unauthenticated, so the fingerprint
        does not depend on a resolvable bearer token — it is reachable on a
        freshly-registered target before its Vault secret is configured.

        On transport or status failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message — the same pattern the Harbor / SDDC
        Manager / NSX connectors established.

        ``operator`` exists for ABC parity with the G0.16-T4 (#1306)
        widening of the fingerprint surface. ArgoCD's version probe is
        unauthenticated, so a system-context call suffices and no
        per-operator Vault read is needed here.
        """
        del operator  # /api/version is unauthenticated — no per-operator read needed
        probed_at = datetime.now(UTC)
        try:
            payload = await self._get_version_unauthenticated(target)
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="argoproj",
                product="argocd",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        return FingerprintResult(
            vendor="argoproj",
            product="argocd",
            version=payload.get("Version") or None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "BuildDate": payload.get("BuildDate"),
                "KustomizeVersion": payload.get("KustomizeVersion"),
                "HelmVersion": payload.get("HelmVersion"),
                "KubectlVersion": payload.get("KubectlVersion"),
            },
        )

    async def probe(self, target: ArgoCdTargetLike) -> ProbeResult:
        """Reachability check delegating to :meth:`fingerprint`.

        Same precedent as the SDDC Manager / NSX connectors: ArgoCD exposes
        no dedicated composite health endpoint, so the unauthenticated
        ``GET /api/version`` probe doubles as the reachability surface. A
        reachable fingerprint maps to ``ProbeResult(ok=True)``; an
        unreachable one carries the fingerprint's structured error string
        as ``reason``.
        """
        probed_at = datetime.now(UTC)
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=probed_at)
        return ProbeResult(
            ok=False,
            reason=str(fp.extras.get("error")) if fp.extras.get("error") else "unreachable",
            probed_at=probed_at,
        )

    async def execute(
        self,
        target: ArgoCdTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`HarborConnector.execute`'s shape. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``, the CLI verbs
        once G3.12-T3 lands) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"argocd-api-3.x"`` → (product=``"argocd"``, version=``"3.x"``,
        impl_id=``"argocd-api"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:argocd-api-connector-shim",
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=UUID(int=0),
            tenant_role=TenantRole.OPERATOR,
        )
        connector_id = f"{self.impl_id}-{self.version}"
        return await dispatch(
            operator=operator,
            connector_id=connector_id,
            op_id=op_id,
            target=target,
            params=params,
        )

    async def aclose(self) -> None:
        """Clear the cached token, then tear down the httpx pool.

        No server-side session to revoke — the bearer token is a static
        credential. The cache is cleared so a post-aclose reuse of the same
        connector instance starts clean.
        """
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
