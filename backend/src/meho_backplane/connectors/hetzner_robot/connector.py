# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""HetznerRobotConnector — hand-rolled HttpConnector subclass for Hetzner Robot.

Skeleton-only — auth + fingerprint + probe + the G0.6 dispatch shim.
Operations arrive in T8 via G0.7 spec ingestion against the Robot Webservice
OpenAPI spec into the ``endpoint_descriptor`` table.

Registered against the v2 registry at module-import time via
:func:`~meho_backplane.connectors.registry.register_connector_v2` in
:mod:`meho_backplane.connectors.hetzner_robot.__init__`.

Auth
----

Hetzner Robot uses HTTP Basic auth against the **Webservice user** — a
distinct account from the Robot login user that must be created in the Robot
portal.  Credentials are loaded once from Vault via an injectable loader and
cached per-target.  The ``Authorization: Basic`` header is recomputed on each
``auth_headers()`` call from the cached values.

IP-block protection
-------------------

Hetzner Robot **blocks the source IP for 10 minutes after 3 failed 401
responses**.  Because MEHO operates on a shared egress IP, a single
misconfigured target could lock every operator off the Robot API for 10
minutes.  The connector therefore raises :exc:`RuntimeError` with an
``auth_failed`` label and a remediation message on the **first** 401 response
— it never retries, never consumes the 2 remaining attempts.

The base :meth:`HttpConnector._retryable` already excludes 4xx from the
tenacity retry predicate.  This connector adds an explicit 401-before-raise
check in ``_request_robot`` so the failure is surfaced with a useful message
before the HTTP status error propagates.

Form-encoded helper
-------------------

The Robot Webservice API requires ``application/x-www-form-urlencoded``
bodies for all write verbs — it rejects ``application/json``.  The
``_post_form`` helper wraps httpx's ``data=`` parameter so callers never pass
a raw ``json=`` arg against this API.  v0.2 read operations never POST, but
the helper ships now for v0.2.next write readiness per the task body.

Fingerprint
-----------

``GET /server`` returns the list of dedicated servers owned by the account.
The connector uses the response to derive ``vendor="hetzner"``,
``product="robot-webservice"``, the account's server count, and the
account ID extracted from the first server's ``server_ip`` field (or the
first 401-not-retried result if unauthenticated).  ``extras["account_id"]``
and ``extras["server_count"]`` are set when the probe succeeds.

Probe
-----

TCP reachability check (implicit via httpx ``AsyncClient``) + TLS cert
validation + ``GET /server`` (the cheapest authenticated endpoint).  A 401
during probe is **not** retried — ``ok=False`` with the auth_failed reason.

Operations
----------

This module ships zero operations — the G0.6 dispatch shim :meth:`execute`
exists for ABC compatibility.  Operations land via T8 spec ingestion.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.hetzner_robot.session import (
    HetznerRobotCredentialsLoader,
    HetznerRobotTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["HetznerRobotConnector"]

_log = structlog.get_logger(__name__)

# Instructional message appended to auth_failed errors so operators know
# which credential to fix without having to read the source.  The 10-minute
# IP block risk is called out explicitly — operators may not be aware of
# Hetzner Robot's unusual shared-egress policy.
_AUTH_FAILED_HINT = (
    "Check the Webservice-user credentials at the target's secret_ref Vault "
    "path. NOTE: Hetzner Robot blocks the source IP for 10 minutes after 3 "
    "consecutive 401 responses — this connector fails fast on the first 401 "
    "to protect shared-egress operators."
)


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is ``SHARED_SERVICE_ACCOUNT`` or unset (pre-G0.3 ``None``)."""
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _basic_auth_header(username: str, password: str) -> str:
    """Compute the ``Authorization: Basic`` header value for *username*:*password*."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


class HetznerRobotConnector(HttpConnector):
    """Hetzner Robot Webservice connector with HTTP Basic auth.

    Per-target credentials cached in :attr:`_creds_cache` (loaded once via
    the injectable :class:`HetznerRobotCredentialsLoader`). HTTP Basic auth
    is sent on every request via ``Authorization: Basic <base64>`` — no
    session token is established.

    **A 401 response is never retried.**  Three consecutive 401s from a single
    source IP trigger a 10-minute IP block on Hetzner Robot.  On the first 401,
    this connector raises :exc:`RuntimeError` with an instructional message
    naming the target and the Vault path to check.

    The :attr:`priority` is set to ``1`` so a future ``GenericRestConnector``
    auto-shim (priority=0) loses the registry tie-break if both register for
    the same triple.
    """

    product = "hetzner-robot"
    version = "2026.04"
    impl_id = "hetzner-rest"
    supported_version_range = None  # Webservice API versioned by date, not semver
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: HetznerRobotCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        self._creds_cache: dict[str, dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: HetznerRobotCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    async def auth_headers(
        self, target: HetznerRobotTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return ``{"Authorization": "Basic ..."}`` for the request.

        Loads credentials from Vault on first call against *target*, caches
        them, and reuses the cached values on subsequent calls.  ``operator``
        is accepted for the shared HTTP auth surface (G3.9-T1) but unused —
        :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` authenticates with a
        Vault-sourced Webservice-user credential, not the operator's OIDC
        token.  Threading the operator into Hetzner Robot's credential loader
        is #G3.10.

        Raises :exc:`NotImplementedError` if ``target.auth_model`` is anything
        other than ``shared_service_account`` or ``None``.
        """
        del operator  # SHARED_SERVICE_ACCOUNT mode does not forward operator identity
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"HetznerRobotConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target)
        return {"Authorization": _basic_auth_header(creds["username"], creds["password"])}

    async def _load_credentials(self, target: HetznerRobotTargetLike) -> dict[str, str]:
        """Return the cached credentials for *target*, loading from Vault on first use.

        The lock serialises concurrent first-use callers for the same target.
        The loaded dict must contain ``"username"`` and ``"password"`` keys;
        missing keys raise :exc:`RuntimeError` naming the target and the missing
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
                    f"hetzner-robot credentials loader for target {target.name!r} returned "
                    f"a dict missing required key {exc.args[0]!r}; need "
                    "{'username': str, 'password': str}"
                ) from exc
            self._creds_cache[target.name] = raw
            _log.info(
                "hetzner_robot_credentials_loaded",
                target=target.name,
                host=target.host,
            )
            return raw

    async def _get_robot_json(
        self,
        target: HetznerRobotTargetLike,
        path: str,
    ) -> Any:
        """Perform a GET against the Robot Webservice, raising auth_failed on 401.

        Wraps :meth:`HttpConnector._get_json` with an explicit 401-intercept
        so operators see the instructional error before httpx's generic
        ``HTTPStatusError`` propagates.  The base ``_retryable`` predicate
        already excludes 4xx, so tenacity does not retry — this method adds
        the human-readable failure on top.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, synthesise_system_operator())
        resp = await client.request("GET", path, headers=headers)
        if resp.status_code == 401:
            raise RuntimeError(
                f"auth_failed: Hetzner Robot returned 401 for target "
                f"{target.name!r} (host={target.host!r}). {_AUTH_FAILED_HINT}"
            )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Non-JSON response from {path}: {resp.status_code} {resp.text[:200]}"
            ) from exc

    async def _post_form(
        self,
        target: HetznerRobotTargetLike,
        path: str,
        data: dict[str, Any],
    ) -> Any:
        """Non-retried POST with ``application/x-www-form-urlencoded`` body.

        The Robot Webservice API requires form-encoded bodies for all write
        verbs.  httpx's ``data=`` parameter encodes a dict as
        ``application/x-www-form-urlencoded`` (RFC 3986 percent-encoding).
        A 401 during a form POST is intercepted and raised as ``auth_failed``
        — same discipline as :meth:`_get_robot_json`.

        v0.2 read operations never POST against the Robot API; this helper
        ships for v0.2.next write readiness per the task body.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, synthesise_system_operator())
        resp = await client.request("POST", path, data=data, headers=headers)
        if resp.status_code == 401:
            raise RuntimeError(
                f"auth_failed: Hetzner Robot returned 401 for target "
                f"{target.name!r} (host={target.host!r}). {_AUTH_FAILED_HINT}"
            )
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Non-JSON response from {path}: {resp.status_code} {resp.text[:200]}"
            ) from exc

    async def fingerprint(self, target: HetznerRobotTargetLike) -> FingerprintResult:
        """Canonical fingerprint built from ``GET /server``.

        Returns ``vendor="hetzner"``, ``product="robot-webservice"``,
        ``extras["account_id"]`` (extracted from the first server entry, or
        ``None`` when the account has no servers), and
        ``extras["server_count"]`` (number of dedicated servers visible to
        the Webservice user).

        On transport, status, or auth failure, returns a non-reachable
        :class:`FingerprintResult` whose ``extras["error"]`` carries the
        exception class + message.
        """
        probed_at = datetime.now(UTC)
        try:
            payload = await self._get_robot_json(target, "/server")
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return FingerprintResult(
                vendor="hetzner",
                product="robot-webservice",
                reachable=False,
                probed_at=probed_at,
                probe_method="GET /server",
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        # The Robot API wraps list responses as {"servers": [...]} or
        # returns the list directly depending on the endpoint version.
        # Normalise both shapes.
        servers: list[dict[str, Any]] = []
        if isinstance(payload, dict) and "servers" in payload:
            servers = payload["servers"]
        elif isinstance(payload, list):
            servers = payload

        account_id: str | None = None
        if servers:
            # account_id is not a first-class field in the Robot API; the
            # Webservice URL itself encodes the account implicitly. Use the
            # first server's numeric id as a stable account fingerprint.
            first = servers[0]
            server_obj = first.get("server", first)
            account_id = str(server_obj.get("server_number") or server_obj.get("id") or "")
            account_id = account_id or None

        return FingerprintResult(
            vendor="hetzner",
            product="robot-webservice",
            reachable=True,
            probed_at=probed_at,
            probe_method="GET /server",
            extras={
                "account_id": account_id,
                "server_count": len(servers),
            },
        )

    async def probe(self, target: HetznerRobotTargetLike) -> ProbeResult:
        """Reachability check via TCP + TLS + ``GET /server`` (401-not-retried).

        Returns ``ok=True`` when the authenticated GET succeeds.
        Returns ``ok=False`` with ``reason`` on any transport, TLS, auth, or
        status failure.  A 401 is captured as a one-shot failure (no retry)
        per the IP-block protection contract.
        """
        probed_at = datetime.now(UTC)
        try:
            await self._get_robot_json(target, "/server")
        except (httpx.HTTPError, OSError, RuntimeError) as exc:
            return ProbeResult(
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                probed_at=probed_at,
            )
        return ProbeResult(ok=True, probed_at=probed_at)

    async def execute(
        self,
        target: HetznerRobotTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`HarborConnector.execute`'s shape.  Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``) construct a real
        :class:`Operator` and call :func:`meho_backplane.operations.dispatch`
        directly — they don't reach this method.

        The connector's natural key is encoded as the dispatcher's
        ``connector_id``: ``"hetzner-rest-2026.04"`` →
        (product=``"hetzner-robot"``, version=``"2026.04"``,
        impl_id=``"hetzner-rest"``).
        """
        from uuid import UUID

        from meho_backplane.auth.operator import Operator, TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:hetzner-rest-connector-shim",
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
        """Clear cached credentials, then tear down the httpx pool."""
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
