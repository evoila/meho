# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""RabbitMqConnector — hand-rolled HttpConnector subclass for RabbitMQ (#2233).

A **read-only** connector over the RabbitMQ Management HTTP API
(``/api``, port 15672 for HTTP / 15671 for HTTPS). It gives an agent a
policy-gated, audited window into a broker's cluster health, messaging
topology, live connectivity, and — the reason it exists — the cross-site
shovel / federation topology, without the operator having to reach for
``rabbitmqctl`` or the management UI.

Registered against the v2 registry at import time (versioned + wildcard)
in :mod:`meho_backplane.connectors.rabbitmq.__init__`; the typed ops are
upserted at lifespan startup via the queued registrar.

Key contracts:

* **Read-only by construction** — beyond registering only
  ``safety_level="safe"`` GET ops, :func:`_assert_read_method` refuses any
  verb other than ``GET`` / ``HEAD`` **before** the request is issued, so
  the ``rabbitmq.request`` passthrough can never mutate the broker.
* **Auth** — the Management plugin uses HTTP Basic. The connector reads a
  ``username`` / ``password`` pair from the target's ``secret_ref`` (a
  KV-v2 path) via the injectable :class:`RabbitMqCredentialsLoader`,
  caches it per target, and sends ``Authorization: Basic`` on each call.
  It locks to :attr:`AuthModel.SHARED_SERVICE_ACCOUNT` (or ``None``) — the
  Harbor / ArgoCD boundary.
* **Credential redaction** — the shovel / federation / parameter /
  definitions surfaces echo back stored ``amqp://user:pass@`` URIs (and,
  for definitions, user ``password_hash`` values). Those handlers run
  their result through
  :func:`~meho_backplane.connectors.rabbitmq.redact.redact_rabbitmq_payload`
  (see
  :data:`~meho_backplane.connectors.rabbitmq.ops.RABBITMQ_REDACTED_OP_IDS`).
* **Fingerprint / probe** — :meth:`fingerprint` reads ``/api/overview`` +
  ``/api/nodes`` for ``rabbitmq_version`` (canonical), cluster name,
  erlang version, and a per-node running/type summary. Both endpoints
  require auth, so an operator-less call falls back to the synthesised
  system operator and fails closed (``reachable=False``). :meth:`probe`
  delegates to :meth:`fingerprint`.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import structlog

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import VaultClientError
from meho_backplane.connectors._shared.cache_key import target_cache_key
from meho_backplane.connectors._shared.system_operator import (
    is_system_operator,
    synthesise_system_operator,
)
from meho_backplane.connectors._shared.vault_creds import VaultCredentialsReadError
from meho_backplane.connectors.adapters.http import HttpConnector
from meho_backplane.connectors.rabbitmq.ops import (
    RABBITMQ_OPS,
    RABBITMQ_WHEN_TO_USE_BY_GROUP,
)
from meho_backplane.connectors.rabbitmq.redact import redact_rabbitmq_payload
from meho_backplane.connectors.rabbitmq.session import (
    RabbitMqCredentialsLoader,
    RabbitMqTargetLike,
    load_credentials_from_vault,
)
from meho_backplane.connectors.schemas import (
    AuthModel,
    FingerprintResult,
    OperationResult,
    ProbeResult,
)

__all__ = ["RabbitMqConnector", "RabbitMqMethodNotAllowedError"]

_log = structlog.get_logger(__name__)

#: The two verbs the read-only connector permits on the wire.
_READ_METHODS: frozenset[str] = frozenset({"GET", "HEAD"})

_OVERVIEW_PATH = "/api/overview"
_NODES_PATH = "/api/nodes"
_PROBE_METHOD = f"GET {_OVERVIEW_PATH} + {_NODES_PATH}"


class RabbitMqMethodNotAllowedError(ValueError):
    """A non-GET/HEAD verb was requested against the read-only connector.

    Raised by :func:`_assert_read_method` **before** any wire call so a
    mutating request is refused inside the process — the code-level arm of
    the read-only guarantee. Subclasses :exc:`ValueError` so the
    dispatcher's ``connector_error`` branch records it cleanly.
    """


def _assert_read_method(method: str) -> str:
    """Return the normalised verb, or raise if it is not GET/HEAD.

    The method gate. Runs before the request is built, so a refused verb
    never reaches the upstream broker.
    """
    normalized = method.strip().upper()
    if normalized not in _READ_METHODS:
        raise RabbitMqMethodNotAllowedError(
            f"RabbitMqConnector is read-only: only {sorted(_READ_METHODS)} are "
            f"permitted; got {method!r}"
        )
    return normalized


def _basic_auth_header(username: str, password: str) -> str:
    """Compute the ``Authorization: Basic`` header value for *username*:*password*."""
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return f"Basic {encoded}"


def _is_acceptable_auth_model(value: Any) -> bool:
    """Return ``True`` iff *value* is the SHARED_SERVICE_ACCOUNT mode or unset.

    Accepts the enum member, the equivalent string, and ``None`` (the
    "auth_model column not yet populated" sentinel for pre-G0.3 targets).
    Same predicate the Harbor / ArgoCD precedents use; inlined to keep
    connectors decoupled.
    """
    if value is None:
        return True
    if value is AuthModel.SHARED_SERVICE_ACCOUNT:
        return True
    return bool(value == AuthModel.SHARED_SERVICE_ACCOUNT.value)


def _summarise_nodes(nodes: Any) -> list[dict[str, Any]]:
    """Reduce ``/api/nodes`` to the per-node fingerprint summary.

    Each entry keeps ``name`` / ``running`` / ``type`` (disc|ram) and the
    per-node ``erlang_version`` when the release reports it. Non-dict
    entries (a malformed payload) are skipped rather than raising.
    """
    if not isinstance(nodes, list):
        return []
    summary: list[dict[str, Any]] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        summary.append(
            {
                "name": node.get("name"),
                "running": node.get("running"),
                "type": node.get("type"),
                "erlang_version": node.get("erlang_version"),
            }
        )
    return summary


class RabbitMqConnector(HttpConnector):
    """RabbitMQ Management HTTP API connector — read-only, HTTP Basic auth.

    Registry v2 triple ``("rabbitmq", "3.x", "rabbitmq-management")``. The
    per-target ``username`` / ``password`` pair is cached in
    :attr:`_creds_cache` keyed on the tenant-unique ``(tenant_id, id)``
    tuple (#1642/#1672), loaded once via the injectable
    :class:`RabbitMqCredentialsLoader`. :attr:`priority` is ``1`` so a
    future ``GenericRestConnector`` auto-shim loses the registry tie-break.
    """

    product = "rabbitmq"
    version = "3.x"
    impl_id = "rabbitmq-management"
    supported_version_range = ">=3.8,<5.0"
    priority = 1

    def __init__(
        self,
        *,
        credentials_loader: RabbitMqCredentialsLoader | None = None,
    ) -> None:
        super().__init__()
        # Keyed on the tenant-unique ``(tenant_id, id)`` tuple so two
        # same-named targets in different tenants never share cached
        # credentials (#1642/#1672).
        self._creds_cache: dict[tuple[str, str], dict[str, str]] = {}
        self._creds_lock = asyncio.Lock()
        self._credentials_loader: RabbitMqCredentialsLoader = (
            credentials_loader if credentials_loader is not None else load_credentials_from_vault
        )

    # Auth

    async def auth_headers(self, target: RabbitMqTargetLike, operator: Operator) -> dict[str, str]:
        """Return ``{"Authorization": "Basic <base64>"}`` for the request.

        Loads the ``username`` / ``password`` from Vault on first call
        against *target*, caches them, and reuses the cached values after.
        The full ``operator`` is forwarded to :meth:`_load_credentials` so
        the live default loader reads the per-target secret under the
        operator's identity. Raises :exc:`NotImplementedError` if
        ``target.auth_model`` is anything other than
        ``shared_service_account`` or ``None``.
        """
        auth_model = getattr(target, "auth_model", None)
        if not _is_acceptable_auth_model(auth_model):
            raise NotImplementedError(
                f"RabbitMqConnector only supports auth_model="
                f"{AuthModel.SHARED_SERVICE_ACCOUNT.value!r}; target "
                f"{target.name!r} requested auth_model={auth_model!r}"
            )
        creds = await self._load_credentials(target, operator)
        return {"Authorization": _basic_auth_header(creds["username"], creds["password"])}

    async def _load_credentials(
        self, target: RabbitMqTargetLike, operator: Operator
    ) -> dict[str, str]:
        """Return the cached credentials for *target*, loading on first use.

        The loaded dict must contain ``username`` + ``password``; a missing
        key raises :exc:`RuntimeError` naming the target. The cache
        fast-path is closed to the synthesised system operator so a
        system/operator-less caller always re-runs the loader (its
        fail-closed guard applies) and can never be served a warm
        credential a real operator primed (#1008).
        """
        cache_key = target_cache_key(target)
        async with self._creds_lock:
            cached = self._creds_cache.get(cache_key)
            if cached is not None and not is_system_operator(operator):
                return cached
            raw = await self._credentials_loader(target, operator)
            missing = [k for k in ("username", "password") if k not in raw]
            if missing:
                raise RuntimeError(
                    f"rabbitmq credentials loader for target {target.name!r} returned a "
                    f"dict missing required key(s) {missing!r}; need "
                    "{'username': str, 'password': str}"
                )
            self._creds_cache[cache_key] = raw
            _log.info("rabbitmq_credentials_loaded", target=target.name, host=target.host)
            return raw

    # Read primitives (GET/HEAD only)

    async def _read(
        self,
        target: RabbitMqTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        redact: bool = False,
    ) -> Any:
        """Retried ``GET`` returning parsed JSON, optionally redacted.

        RabbitMQ collection endpoints return a bare JSON array while
        ``/api/overview`` + ``/api/definitions`` return an object, so the
        return type is ``Any`` — the dispatcher / reducer accept both.
        """
        payload: Any = await self._get_json(target, path, operator=operator, params=params)
        return redact_rabbitmq_payload(payload) if redact else payload

    async def _read_vhost_scoped(
        self,
        target: RabbitMqTargetLike,
        base_path: str,
        params: dict[str, Any],
        *,
        operator: Operator,
        redact: bool = False,
    ) -> Any:
        """GET *base_path*, appending a percent-encoded ``/{vhost}`` when supplied."""
        vhost = params.get("vhost")
        path = base_path if not vhost else f"{base_path}/{quote(str(vhost), safe='')}"
        return await self._read(target, path, operator=operator, redact=redact)

    async def _head(
        self,
        target: RabbitMqTargetLike,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Issue a ``HEAD`` and return a status/header summary (no body).

        Used only by the passthrough op; the base helpers return a JSON
        body, which a HEAD response never carries.
        """
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        resp = await client.head(
            path,
            params=params,
            headers=headers,
            extensions=self._request_extensions(target),
        )
        resp.raise_for_status()
        return {"status_code": resp.status_code, "headers": dict(resp.headers)}

    # Fingerprint / probe

    async def fingerprint(
        self,
        target: RabbitMqTargetLike,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from ``GET /api/overview`` + ``GET /api/nodes``.

        ``rabbitmq_version`` becomes the canonical ``version``; the cluster
        name, erlang / management versions, product name/version, and the
        per-node running/type summary land under ``extras``. Both endpoints
        require Basic auth, so an operator-less call falls back to the
        synthesised system operator and fails closed at the live Vault read
        (``reachable=False`` with ``extras["error"]``) rather than raising.
        """
        probed_at = datetime.now(UTC)
        effective_operator = operator or synthesise_system_operator()
        try:
            overview = await self._read(target, _OVERVIEW_PATH, operator=effective_operator)
            nodes = await self._read(target, _NODES_PATH, operator=effective_operator)
        except (
            httpx.HTTPError,
            OSError,
            RuntimeError,
            VaultClientError,
            VaultCredentialsReadError,
        ) as exc:
            return FingerprintResult(
                vendor="rabbitmq",
                product="rabbitmq",
                reachable=False,
                probed_at=probed_at,
                probe_method=_PROBE_METHOD,
                extras={"error": f"{type(exc).__name__}: {exc}"},
            )
        overview_map = overview if isinstance(overview, dict) else {}
        return FingerprintResult(
            vendor="rabbitmq",
            product="rabbitmq",
            version=overview_map.get("rabbitmq_version") or None,
            reachable=True,
            probed_at=probed_at,
            probe_method=_PROBE_METHOD,
            extras={
                "cluster_name": overview_map.get("cluster_name"),
                "erlang_version": overview_map.get("erlang_version"),
                "management_version": overview_map.get("management_version"),
                "product_name": overview_map.get("product_name"),
                "product_version": overview_map.get("product_version"),
                "nodes": _summarise_nodes(nodes),
            },
        )

    async def probe(self, target: RabbitMqTargetLike) -> ProbeResult:
        """Reachability check delegating to :meth:`fingerprint`.

        The overview/nodes read doubles as the reachability surface (the
        Harbor/ArgoCD precedent). Operator-less, so it fails closed at the
        Vault read — RabbitMQ's authenticated API is never reachable
        without operator context, and the probe is honest about that.
        """
        probed_at = datetime.now(UTC)
        fp = await self.fingerprint(target)
        if fp.reachable:
            return ProbeResult(ok=True, probed_at=probed_at)
        error = fp.extras.get("error")
        return ProbeResult(
            ok=False,
            reason=str(error) if error else "unreachable",
            probed_at=probed_at,
        )

    # Curated read-op handlers (each a thin GET; operator threaded by name)

    async def overview(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.overview`` — GET /api/overview."""
        del params
        return await self._read(target, _OVERVIEW_PATH, operator=operator)

    async def nodes(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.nodes`` — GET /api/nodes."""
        del params
        return await self._read(target, _NODES_PATH, operator=operator)

    async def exchanges(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.exchanges`` — GET /api/exchanges[/{vhost}]."""
        return await self._read_vhost_scoped(target, "/api/exchanges", params, operator=operator)

    async def queues(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.queues`` — GET /api/queues[/{vhost}]."""
        return await self._read_vhost_scoped(target, "/api/queues", params, operator=operator)

    async def bindings(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.bindings`` — GET /api/bindings[/{vhost}]."""
        return await self._read_vhost_scoped(target, "/api/bindings", params, operator=operator)

    async def vhosts(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.vhosts`` — GET /api/vhosts."""
        del params
        return await self._read(target, "/api/vhosts", operator=operator)

    async def connections(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.connections`` — GET /api/connections."""
        del params
        return await self._read(target, "/api/connections", operator=operator)

    async def channels(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.channels`` — GET /api/channels."""
        del params
        return await self._read(target, "/api/channels", operator=operator)

    async def consumers(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.consumers`` — GET /api/consumers."""
        del params
        return await self._read(target, "/api/consumers", operator=operator)

    async def shovels(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.shovels`` — GET /api/parameters/shovel[/{vhost}] (redacted)."""
        return await self._read_vhost_scoped(
            target, "/api/parameters/shovel", params, operator=operator, redact=True
        )

    async def shovel_status(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.shovel_status`` — GET /api/shovels[/{vhost}] (redacted)."""
        return await self._read_vhost_scoped(
            target, "/api/shovels", params, operator=operator, redact=True
        )

    async def federation_links(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.federation_links`` — GET /api/federation-links (redacted)."""
        del params
        return await self._read(target, "/api/federation-links", operator=operator, redact=True)

    async def parameters(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.parameters`` — GET /api/parameters (redacted)."""
        del params
        return await self._read(target, "/api/parameters", operator=operator, redact=True)

    async def policies(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.policies`` — GET /api/policies[/{vhost}]."""
        return await self._read_vhost_scoped(target, "/api/policies", params, operator=operator)

    async def definitions(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.definitions`` — GET /api/definitions (redacted)."""
        del params
        return await self._read(target, "/api/definitions", operator=operator, redact=True)

    async def request_passthrough(
        self, operator: Operator, target: RabbitMqTargetLike, params: dict[str, Any]
    ) -> Any:
        """``rabbitmq.request`` — GET/HEAD an arbitrary path (method-gated, redacted).

        The verb is checked by :func:`_assert_read_method` before any wire
        call, so a non-GET/HEAD request is refused inside the process.
        """
        method = _assert_read_method(str(params.get("method", "GET")))
        path = str(params["path"])
        query = params.get("query")
        if method == "HEAD":
            payload: Any = await self._head(target, path, operator=operator, params=query)
        else:
            payload = await self._get_json(target, path, operator=operator, params=query)
        return redact_rabbitmq_payload(payload)

    # Registration + dispatch shim

    @classmethod
    async def register_operations(cls) -> None:
        """Upsert every op in :data:`RABBITMQ_OPS` into ``endpoint_descriptor``.

        Called from the application lifespan (via the registrar queued in
        the package ``__init__``) after the registry has eager-imported
        every connector module. Idempotent across pod restarts — mirrors
        the Harbor / ArgoCD shape.
        """
        from meho_backplane.operations.typed_register import register_typed_operation

        for op in RABBITMQ_OPS:
            handler = getattr(cls, op.handler_attr, None)
            if handler is None:
                raise AttributeError(
                    f"RabbitMqConnector op {op.op_id!r} declares "
                    f"handler_attr={op.handler_attr!r} but the class has no such attribute"
                )
            when_to_use = RABBITMQ_WHEN_TO_USE_BY_GROUP.get(op.group_key)
            if when_to_use is None:
                raise ValueError(
                    f"RabbitMqConnector op {op.op_id!r} declares group_key="
                    f"{op.group_key!r} but no curated when_to_use exists for that key. "
                    f"Add an entry to RABBITMQ_WHEN_TO_USE_BY_GROUP in ops.py."
                )
            await register_typed_operation(
                product=cls.product,
                version=cls.version,
                impl_id=cls.impl_id,
                op_id=op.op_id,
                handler=handler,
                summary=op.summary,
                description=op.description,
                parameter_schema=op.parameter_schema,
                response_schema=op.response_schema,
                group_key=op.group_key,
                when_to_use=when_to_use,
                tags=list(op.tags),
                safety_level=op.safety_level,
                requires_approval=op.requires_approval,
                llm_instructions=op.llm_instructions,
            )
        _log.info(
            "rabbitmq_operations_registered",
            count=len(RABBITMQ_OPS),
            product=cls.product,
            version=cls.version,
            impl_id=cls.impl_id,
        )

    async def execute(
        self,
        target: RabbitMqTargetLike,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Legacy shim — delegates to the G0.6 dispatcher.

        Mirrors :meth:`HarborConnector.execute`. Post-G0.6 callers
        (``/api/v1/operations/call``, MCP ``call_operation``) construct a
        real :class:`Operator` and call
        :func:`meho_backplane.operations.dispatch` directly.
        """
        from uuid import UUID

        from meho_backplane.auth.operator import TenantRole
        from meho_backplane.operations import dispatch

        operator = Operator(
            sub="system:rabbitmq-management-connector-shim",
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

        No server-side session to revoke — HTTP Basic is stateless. The
        cache is cleared so a post-aclose reuse of the same connector
        instance starts clean.
        """
        async with self._creds_lock:
            self._creds_cache.clear()
        await super().aclose()
