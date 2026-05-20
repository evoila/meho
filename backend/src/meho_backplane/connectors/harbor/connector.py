# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""HarborConnector — hand-rolled HttpConnector subclass for Harbor 2.x.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in #620 via G0.7 spec ingestion against the Harbor 2.x
OpenAPI spec into the ``endpoint_descriptor`` table.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.harbor.__init__`. The G0.7 auto-shim's
idempotency check (in
:func:`~meho_backplane.operations.ingest.connector_registration.ensure_connector_class_registered`
once #408's pipeline lands in main) no-ops on subsequent ingests against the
same ``(product="harbor", version="2.x", impl_id="harbor-rest")`` triple.

Auth
----

Harbor uses HTTP Basic auth sent on every request — no session cookie or
XSRF token is established. The connector caches the raw service-account
credentials (loaded once from Vault via an injectable loader) and computes
the ``Authorization: Basic`` header on each :meth:`auth_headers` call.

Harbor supports two account forms:

* **Admin account**: plain username (e.g. ``"admin"``).
* **Robot account**: Harbor-formatted username (e.g. ``"robot$project+name"``
  for a project-scoped robot or ``"robot$name"`` for a system-level robot).

Both forms are stored verbatim in Vault under the target's ``secret_ref``
path. No reformatting is applied; the stored username is sent as-is.

This differs from the SDDC Manager precedent in that no ``sso_realm`` suffix
is appended — Harbor's Basic auth header carries ``username:password`` directly.

Auth model gating
-----------------

v0.2 locks the connector to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or
``None`` for pre-G0.3 targets where the column hasn't been populated yet).
:meth:`auth_headers` rejects any other ``target.auth_model`` value with a
clear :exc:`NotImplementedError` naming both the target and the requested mode.

Fingerprint
-----------

``GET /api/v2.0/systeminfo`` returns a ``GeneralInfo`` object. The
``harbor_version`` field carries the full version string (e.g.
``"v2.11.0-abc1234"``); the connector splits on the first ``-`` to extract
separate ``version`` (``"v2.11.0"``) and ``build`` (``"abc1234"``) values.
``extras["auth_mode"]`` carries the Harbor auth mode (e.g. ``"db_auth"``,
``"ldap_auth"``, ``"oidc_auth"``).

Probe
-----

``GET /api/v2.0/health`` is Harbor's own composite healthcheck covering DB,
redis, registry, jobservice, and related subsystems. The connector maps the
per-component ``status`` fields to a single ``ok`` boolean + a ``reason``
string listing any unhealthy component names.

This differs from the SDDC Manager / NSX precedents that delegate ``probe()``
to ``fingerprint()``. Harbor's health endpoint is purpose-built for
reachability checks and covers subsystem state that ``systeminfo`` does not
expose, making the dedicated endpoint the better choice.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim :meth:`execute`
exists for ABC compatibility but operations land in the ``endpoint_descriptor``
table via #620's spec ingestion. Until then, the connector is registered and
discoverable but ``execute(target, op_id, ...)`` against any ``op_id``
resolves to "unknown operation" at the dispatcher layer — which is the correct
behaviour for a registered-but-empty connector at this Task's stage.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.harbor.session import (
    HarborCredentialsLoader,
    HarborTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["HarborConnector"]

_log = structlog.get_logger(__name__)


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3 targets).
    Any other value is rejected by the caller. Same predicate the SDDC
    Manager and NSX precedents use; lifted into this module to keep
    connectors decoupled.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _basic_auth_header(username: str, password: str) -> str:
    """Compute the ``Authorization: Basic`` header value for *username*:*password*."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def _parse_harbor_version(harbor_version: str) -> tuple[str | None, str | None]:
    """Split ``harbor_version`` into (version, build).

    ``harbor_version`` from ``GET /api/v2.0/systeminfo`` is typically
    ``"v2.11.0-abc1234"`` (version + git hash) or bare ``"v2.11.0"``.
    Returns ``(version_str, build_str)`` where ``build_str`` is ``None``
    when no ``-`` separator is present.
    """
    if not harbor_version:
        return None, None
    if "-" in harbor_version:
        version_str, build_str = harbor_version.split("-", 1)
        return version_str or None, build_str or None
    return harbor_version, None


class HarborConnector(HttpConnector):
    """Harbor 2.x REST connector with HTTP Basic auth.

    Per-target credentials cached in :attr:`_creds_cache` (loaded once via
    the injectable :class:`HarborCredentialsLoader`). HTTP Basic auth is sent
    on every request via ``Authorization: Basic <base64>`` — no session
    token is established and no 401-driven re-login is needed.

    The :attr:`priority` is set to ``1`` so a future
    ``GenericRestConnector`` auto-shim that somehow registers for the same
    triple loses the registry's tie-break ladder.
    """

    # G0.6 v2 registry metadata. The (product, version, impl_id) triple
    # matches the dispatcher's parse_connector_id contract:
    # ``"harbor-rest-2.x"`` -> (``"harbor"``, ``"2.x"``, ``"harbor-rest"``).
    product = "harbor"
    version = "2.x"
    impl_id = "harbor-rest"
    supported_version_range = ">=2.0,<3.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: HarborCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[str, dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: HarborCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(self, target: HarborTargetLike, raw_jwt: str) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target*, caches
        them, and reuses the cached values on subsequent calls. ``raw_jwt``
        is accepted for ABC-signature compatibility but unused —
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` authenticates with a
        Vault-sourced service account, not the operator's OIDC token.

        The Basic auth username is sent verbatim from the Vault-loaded
        credentials — no ``sso_realm`` suffix is appended. Both admin
        usernames (``"admin"``) and robot account usernames
        (``"robot$project+name"``) are passed through unchanged.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        del raw_jwt  # SHARED_SERVICE_ACCOUNT mode does not forward operator JWT
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"HarborConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target)
        return {"Authorization": _basic_auth_header(creds["username"], creds["password"])}

    async def _load_credentials(self, target: HarborTargetLike) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same target;
        subsequent calls take the fast path under the same lock. The loaded
        dict must contain ``"username"`` and ``"password"`` keys; missing
        keys raise a :exc:`RuntimeError` naming the target and the missing
        key so operators can identify a misconfigured Vault path.
        """
        async with self._creds_lock:
            cached = self._creds_cache.get(target.name)
            if cached is not None:
                return cached
            raw = await self._credentials_loader(target)
            try:
                _ = raw["username"]
                _ = raw["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"harbor credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[target.name] = raw
            _log.info(
                "harbor_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def fingerprint(self, target: HarborTargetLike) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /api/v2.0/systeminfo``.

        The ``harbor_version`` field is split on the first ``-`` to produce
        separate ``version`` and ``build`` values. ``extras["auth_mode"]``
        carries the Harbor auth mode (``"db_auth"``, ``"ldap_auth"``, etc.).

        On transport or status failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message — same pattern the SDDC Manager and NSX
        connectors established.
        """
        probed_at = datetime.now(UTC)
        try:
            payload = await self._get_json(target, "/api/v2.0/systeminfo", raw_jwt="")
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="vmware",
                product="harbor",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /api/v2.0/systeminfo",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        harbor_version = payload.get("harbor_version") or ""
        version_str, build_str = _parse_harbor_version(harbor_version)
        return FingerprintResult(
            vendor="vmware",
            product="harbor",
            version=version_str,
            build=build_str,
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /api/v2.0/systeminfo",
            extras={
                "auth_mode": payload.get("auth_mode"),
                "registry_url": payload.get("registry_url"),
                "external_url": payload.get("external_url"),
            },
        )

    async def probe(self, target: HarborTargetLike) -> ProbeResult:
        """Composite reachability check via ``GET /api/v2.0/health``.

        Harbor's health endpoint covers DB, redis, registry, jobservice, and
        related subsystems. The connector maps per-component ``status`` fields
        to a single ``ok`` boolean; when any component is unhealthy, ``reason``
        lists the unhealthy component names.

        Unlike the SDDC Manager / NSX precedents that delegate to
        ``fingerprint()``, Harbor's dedicated health endpoint is the
        purpose-built reachability surface — it checks subsystem state that
        ``systeminfo`` does not expose.
        """
        probed_at = datetime.now(UTC)
        try:
            payload = await self._get_json(target, "/api/v2.0/health", raw_jwt="")
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                probed_at=probed_at,
            )
        overall = payload.get("status", "unknown")
        if overall == "healthy":
            return ProbeResult(ok=True, probed_at=probed_at)
        components = payload.get("components") or []
        unhealthy = [c.get("name", "unknown") for c in components if c.get("status") != "healthy"]
        reason = (
            f"unhealthy components: {', '.join(unhealthy)}" if unhealthy else f"status={overall!r}"
        )
        return ProbeResult(ok=False, reason=reason, probed_at=probed_at)

    async def execute(
        self,
        target: HarborTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`SddcManagerConnector.execute`'s shape. Post-G0.6
        callers (``/api/v1/operations/call``, MCP ``call_operation``, the CLI
        verbs once #622 lands) construct a real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly — they don't
        reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id`` per ``parse_connector_id``'s contract:
        ``"harbor-rest-2.x"`` → (product=``"harbor"``,
        version=``"2.x"``, impl_id=``"harbor-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:harbor-rest-connector-shim",
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
        """Clear cached credentials, then tear down the httpx pool.

        No server-side session to revoke — HTTP Basic is stateless.
        The credential cache is cleared so a post-aclose reuse of the same
        connector instance (e.g. a test that builds one connector across two
        contexts) starts clean.
        """
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
