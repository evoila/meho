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

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
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
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: HarborCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def _http_client(self, target: HarborTargetLike) -> httpx.AsyncClient:
        """Return the pooled client with Harbor's session cookie discarded.

        Harbor sets a ``sid`` session cookie on every authenticated
        response. httpx's default cookie jar would store it and replay it
        on the next request, which flips Harbor out of stateless Basic
        auth into session mode — and session mode rejects state-changing
        verbs (POST/PUT/DELETE) that lack an ``X-Harbor-CSRF-Token``
        header with ``403 {"code":"FORBIDDEN","message":"CSRF token not
        found in request"}``. This connector authenticates with HTTP
        Basic on every call by design (see :mod:`.session` — "no session
        token is established"), so the cookie is never wanted: clear the
        jar before each request to keep every call stateless.
        """
        client = await super()._http_client(target)
        client.cookies.clear()
        return client

    async def auth_headers(self, target: HarborTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target*, caches
        them, and reuses the cached values on subsequent calls. The full
        ``operator`` is forwarded to :meth:`_load_credentials` so the live
        default loader (G3.10-T1 #945) reads the per-target Vault secret
        under the operator's identity (``vault_client_for_operator(operator)``).
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` selects the Vault-sourced
        service account once the loader has resolved it; the operator's
        JWT only authenticates the read, not the Harbor request itself.

        The Basic auth username is sent verbatim from the Vault-loaded
        credentials — no ``sso_realm`` suffix is appended. Both admin
        usernames (``"admin"``) and robot account usernames
        (``"robot$project+name"``) are passed through unchanged.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is
        anything other than ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"HarborConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target, operator)
        return {"Authorization": _basic_auth_header(creds["username"], creds["password"])}

    async def _load_credentials(
        self, target: HarborTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same target;
        subsequent calls take the fast path under the same lock. The loaded
        dict must contain ``"username"`` and ``"password"`` keys; missing
        keys raise a :exc:`RuntimeError` naming the target and the missing
        key so operators can identify a misconfigured Vault path.

        ``operator`` is forwarded to the
        :class:`HarborCredentialsLoader` so the default loader can read
        the per-target Vault secret under the operator's identity
        (G3.10-T1's live read). The default loader is the thin
        harbor-specific entry point to the shared operator-context
        Vault read; injected test loaders accept the same
        ``(target, operator)`` pair.

        The cache fast-path is closed to the synthesised system operator
        (``is_system_operator``): a system/operator-less caller always
        runs the loader so its fail-closed guard applies, and can never be
        served warm credentials a real operator primed but it could not
        resolve itself (#1008). Real-operator behaviour is unchanged —
        cold load → cache → reuse.
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            try:
                _ = raw["username"]
                _ = raw["password"]
            except KeyError as exc:
                raise RuntimeError(
                    f"harbor credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[cache_key] = raw
            _log.info(
                "harbor_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def fingerprint(
        self,
        target: HarborTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /api/v2.0/systeminfo``.

        The ``harbor_version`` field is split on the first ``-`` to produce
        separate ``version`` and ``build`` values. ``extras["auth_mode"]``
        carries the Harbor auth mode (``"db_auth"``, ``"ldap_auth"``, etc.).

        On transport or status failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message — same pattern the SDDC Manager and NSX
        connectors established.

        ``operator`` exists for ABC parity with the G0.16-T4 (#1306)
        widening of the K8s/vmware/sddc/NSX fingerprint surface. Harbor
        was not in the v0.8.0 dogfood's affected-targets list; leaving
        the system-context path in place avoids changing behaviour for
        targets that were not observed to misbehave. A future deliberate
        convergence sweep could route the operator through here on the
        same shape as the four affected connectors.
        """
        del operator  # see docstring — out of #1306's scope
        probed_at = datetime.now(UTC)
        try:
            payload = await self._get_json(
                target, "/api/v2.0/systeminfo", operator=synthesise_system_operator()
            )
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
            payload = await self._get_json(
                target, "/api/v2.0/health", operator=synthesise_system_operator()
            )
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

    async def robot_create(
        self,
        operator: Operator,
        target: HarborTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Create a project-scoped robot account via Harbor 2.x.

        Op-id: ``harbor.robot.create``. Classified ``credential_mint``:
        the broadcast collapses to aggregate-only so the minted secret
        never appears in the SSE feed or any Slack mirror channel.

        Uses :meth:`_post_json` (non-retried POST — Harbor's create
        endpoint is non-idempotent; a retry could mint a second account
        with the same name, which Harbor rejects with 409 anyway, but
        the tenacity decorator must not trigger).

        The handler grants push + pull access on the named project via the
        Harbor v2 robot API (``POST /api/v2.0/robots`` with ``level=project``).
        System-level access (omit ``level`` + ``namespace``) is out of scope.

        The dispatched ``operator`` is forwarded to :meth:`_post_json`
        (and onward to :meth:`auth_headers` → :meth:`_load_credentials`)
        so the live default loader reads the per-target service-account
        credential under the operator's identity (the operator-context
        Vault read, G3.10). The dispatcher threads ``operator`` by
        parameter name — see
        :func:`~meho_backplane.operations._branches.dispatch_typed`. The
        operator's JWT authenticates the credential read, not the Harbor
        request itself.

        Parameters
        ----------
        operator
            Request-scoped operator dispatched into the handler. Its
            validated JWT authenticates the per-target Vault credential
            read.
        target
            Resolved Harbor target (must satisfy :class:`HarborTargetLike`).
        params
            Schema-validated: ``name`` (str), ``project`` (str),
            ``duration`` (int, -1 for never-expire).

        Returns
        -------
        dict[str, Any]
            ``{id: int, name: str, secret: str}`` — the secret is the
            minted credential, returned ONLY on creation by Harbor.

        Raises
        ------
        httpx.HTTPStatusError
            On any 4xx/5xx from Harbor (e.g. 409 Conflict if a robot
            with this name already exists in the project). The dispatcher
            catches all exceptions and wraps them as ``connector_error``
            OperationResults.
        """
        name = str(params["name"])
        project = str(params["project"])
        duration = int(params["duration"])

        body: dict[str, Any] = {
            "name": name,
            "duration": duration,
            "disable": False,
            "level": "project",
            "permissions": [
                {
                    "kind": "project",
                    "namespace": project,
                    "access": [
                        {"resource": "repository", "action": "push"},
                        {"resource": "repository", "action": "pull"},
                    ],
                }
            ],
        }
        path = "/api/v2.0/robots"
        result = await self._post_json(target, path, operator=operator, json=body)
        return {
            "id": result["id"],
            "name": result["name"],
            "secret": result["secret"],
        }

    async def robot_delete(
        self,
        operator: Operator,
        target: HarborTargetLike,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """Delete a project-scoped robot account via Harbor 2.x.

        Op-id: ``harbor.robot.delete``. Classified ``write`` (suffix-based).
        No secret material in the response.

        Uses the pooled httpx client directly (non-retried DELETE —
        ``HttpConnector._IDEMPOTENT_METHODS`` is ``{GET, HEAD, OPTIONS}``
        and ``_request_json`` rejects non-idempotent verbs; no
        ``_delete_json`` helper exists on the base class). Permanent
        removal — irreversible.

        The dispatched ``operator`` is forwarded to :meth:`auth_headers`
        (and onward to :meth:`_load_credentials`) so the live default
        loader reads the per-target service-account credential under the
        operator's identity (the operator-context Vault read, G3.10). The
        dispatcher threads ``operator`` by parameter name — see
        :func:`~meho_backplane.operations._branches.dispatch_typed`.

        Parameters
        ----------
        operator
            Request-scoped operator dispatched into the handler. Its
            validated JWT authenticates the per-target Vault credential
            read.
        target
            Resolved Harbor target.
        params
            Schema-validated: ``project`` (str), ``id`` (int ≥ 1).

        Returns
        -------
        dict[str, Any]
            ``{id: int, deleted: True}`` — Harbor returns HTTP 200 with an
            empty body; the ``id`` echo is synthesized for a useful
            agent-facing result.

        Raises
        ------
        httpx.HTTPStatusError
            On any 4xx/5xx from Harbor (e.g. 404 if the robot ID does
            not exist in the named project).
        """
        robot_id = int(params["id"])

        path = f"/api/v2.0/robots/{robot_id}"
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.request("DELETE", path, headers=headers)
        resp.raise_for_status()
        return {"id": robot_id, "deleted": True}

    async def invalidate_credentials(self, target: HarborTargetLike) -> None:
        """Duck-typed credential-eviction hook for the dispatch path (#2396).

        Drops the cached credentials for *target* so the next credential read
        re-reads Vault. Called by the dispatcher on an establish-auth failure
        so an operator's out-of-band restage converges on the next dispatch
        without a backplane restart. Holds :attr:`_creds_lock` so the pop is
        serialised against an in-flight load.
        """
        async with self._creds_lock:
            self._creds_cache.pop(target_cache_key(target), None)

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
