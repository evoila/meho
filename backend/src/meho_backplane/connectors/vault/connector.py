# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VaultConnector — HashiCorp Vault connector reference implementation.

This module is the proof step for the G0.2 abstraction (Initiative #223)
**and** the reference proof that the G0.6 operation substrate (Initiative
#388) round-trips against already-shipped connector code. The
OIDC-login-then-read pattern already implemented in
:mod:`meho_backplane.auth.vault` is reused through module-level references
so the existing test seams (``vault_module._build_client`` monkeypatches)
remain valid without duplication.

Auth model: ``shared_service_account`` — every operator's JWT is
forwarded to Vault's JWT/OIDC auth method bound to the ``meho-mcp``
role. The resulting Vault token is per-request and revoked on context
exit (behaviour inherited from
:func:`~meho_backplane.auth.vault.vault_client_for_operator`).

Target contract (G0.3 #224, operator-aware dispatch G0.8-T3 #629):

The connector is typed against the real
:class:`~meho_backplane.targets.schemas.Target`. It reads Vault
connection parameters (address, role, mount path, namespace, timeout)
from :func:`~meho_backplane.settings.get_settings`, not from the
target, because those are deployment-level settings, not per-operator
overrides — so ``probe``/``fingerprint`` accept ``Target | None`` and
ignore the value entirely. The operator's bearer token is **not** on
the persisted ``Target`` row (a per-request token must not be
persisted on a shared target); typed KV/auth/sys handlers read it from
the request-scoped :class:`~meho_backplane.auth.operator.Operator` the
dispatcher threads (see
:func:`~meho_backplane.operations._branches.dispatch_typed`).

Operation dispatch (post-G0.6-T-Refactor-Vault):

Operations are **no longer** routed through an in-code ``_op_map``.
The full per-op surface lives in the ``endpoint_descriptor`` table;
:func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
is the upsert helper the lifespan calls at startup. Operation execution
flows through :func:`meho_backplane.operations.dispatch`, which handles
parameter validation, the policy gate, audit-log write, broadcast
publish, and JSONFlux reduction in one place. The connector's
:meth:`VaultConnector.execute` method is now a thin shim that delegates
to :func:`dispatch` so operator-less callers
(:mod:`meho_backplane.auth.vault.vault_readiness_probe`) keep working
unchanged while the typed-op pipeline carries the real semantics. The
pre-G0.6 chassis route at ``/api/v1/connectors/{product}/{op_id}`` that
used to depend on this shim was deprecated and removed by G0.6-T11
(#412); the canonical dispatch surface is ``POST /api/v1/operations/call``.
Operator-aware callers (``/api/v1/health`` post-refactor, the
``/api/v1/operations/call_operation`` meta-tool, MCP ``call_operation``)
construct the :class:`~meho_backplane.auth.operator.Operator` and call
:func:`dispatch` directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import hvac.exceptions
import requests.exceptions
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.settings import get_settings
from meho_backplane.targets.schemas import Target

__all__ = ["VaultConnector"]

_log = structlog.get_logger(__name__)


# Fixed UUID used as the synthetic operator's tenant_id when
# :meth:`VaultConnector.execute` is called without an
# :class:`Operator` in scope. Typed registrations are always
# ``tenant_id IS NULL`` in ``endpoint_descriptor`` so the dispatcher's
# lookup falls through to the global row regardless of which tenant_id
# we pass; the value matters only for the audit row's ``tenant_id``
# column. Nil-UUID is the unambiguous "this row came from a system
# call, not a real tenant" sentinel. The constant lives at module
# scope so the audit-log grep for nil-tenant calls is stable across
# deploys.
_SYSTEM_TENANT_ID: UUID = UUID(int=0)
_SYSTEM_OPERATOR_SUB: str = "system:vault-connector-shim"


class VaultConnector(Connector):
    """HashiCorp Vault connector — shared_service_account auth via OIDC.

    Registered against the v2 connector registry as
    ``(product="vault", version="1.x", impl_id="vault")`` so the G0.6
    dispatcher's :func:`~meho_backplane.connectors.resolver.resolve_connector`
    can pick it for a target whose fingerprint reports a Vault 1.x
    version. The v1-style :func:`~meho_backplane.connectors.registry.register_connector`
    entry point is **not** used for this connector post-refactor — the
    parallel v2 entry is the canonical registration.

    :attr:`supported_version_range` is left ``None`` for now; the
    connector handles every shipped Vault release. G3.3 (#366) will
    pin a range when the operator-facing op surface starts depending
    on version-specific KV-v2 semantics.
    """

    product = "vault"
    version = "1.x"
    impl_id = "vault"

    async def fingerprint(
        self,
        target: Target | None,
        operator: Operator | None = None,
    ) -> FingerprintResult:
        """Canonical fingerprint from ``GET /v1/sys/health``.

        Reuses :func:`~meho_backplane.auth.vault._build_client` so the
        existing test seam applies and the vault_timeout / namespace
        settings are respected without duplicating the client-
        construction logic. ``target`` is part of the
        :class:`~meho_backplane.connectors.base.Connector` ABC contract
        but unused — Vault connection params come from settings.
        ``operator`` is also unused: the ``/sys/health`` endpoint is
        unauthenticated, so the route operator has no role here. The
        argument exists for ABC signature parity (G0.16-T4 #1306
        widened the ABC to support per-operator Vault credential reads
        on the connectors that need them).
        """
        del operator  # unused — health endpoint is unauthenticated
        settings = get_settings()
        client = _auth_vault._build_client(settings)
        payload = await _auth_vault._to_thread_read_health(client)

        version: str | None = None
        build: str | None = None
        extras: dict[str, Any] = {}

        if isinstance(payload, dict):
            version = payload.get("version")
            build = payload.get("build_date")
            extras = {
                "cluster_id": payload.get("cluster_id"),
                "cluster_name": payload.get("cluster_name"),
                "sealed": payload.get("sealed"),
                "standby": payload.get("standby"),
                "replication_dr_mode": payload.get("replication_dr_mode"),
                "replication_performance_mode": payload.get("replication_performance_mode"),
            }

        return FingerprintResult(
            vendor="hashicorp",
            product="vault",
            version=version,
            build=build,
            reachable=True,
            probed_at=datetime.now(UTC),
            probe_method="GET /v1/sys/health",
            extras=extras,
        )

    async def probe(self, target: Target | None) -> ProbeResult:
        """Lightweight reachability check via unauthenticated ``/v1/sys/health``.

        Reuses :func:`~meho_backplane.auth.vault._build_client` so the
        test seam (``monkeypatch.setattr(vault_module, "_build_client",
        fake)``) applies to this method as well, keeping the existing
        ``test_auth_vault.py`` suite green after
        ``vault_readiness_probe`` is refactored to delegate here.

        The ``reason`` field carries the same detail strings that
        :func:`~meho_backplane.auth.vault.vault_readiness_probe`
        previously embedded directly (``"sealed=False"``, ``"sealed"``,
        ``"uninitialized"``, ``"http_429"``, etc.) — callers that need
        the old ``detail`` shape map from ``reason``.
        """
        settings = get_settings()
        client = _auth_vault._build_client(settings)
        try:
            payload = await _auth_vault._to_thread_read_health(client)
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
        ) as exc:
            return ProbeResult(
                ok=False,
                reason=f"unreachable: {type(exc).__name__}",
                probed_at=datetime.now(UTC),
            )
        except hvac.exceptions.VaultError as exc:
            return ProbeResult(
                ok=False,
                reason=f"vault_error: {type(exc).__name__}",
                probed_at=datetime.now(UTC),
            )

        ok, detail = _auth_vault._classify_health_response(payload)
        return ProbeResult(ok=ok, reason=detail, probed_at=datetime.now(UTC))

    async def execute(
        self,
        target: Target | None,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Delegate to the G0.6 dispatcher.

        Thin shim that turns the ABC-defined
        ``(target, op_id, params)`` surface into a
        :func:`~meho_backplane.operations.dispatch` call so operator-less
        callers (:mod:`meho_backplane.auth.vault.vault_readiness_probe`,
        and the dispatcher's own ``source_kind == "typed"`` branch)
        get the new substrate's behaviour — parameter validation against
        the registered ``endpoint_descriptor.parameter_schema``,
        policy gate, audit-log write, broadcast publish, JSONFlux
        wrapping — without changing their call sites. The pre-G0.6
        chassis route at ``/api/v1/connectors/{product}/{op_id}`` that
        used to depend on this shim was deprecated and removed by
        G0.6-T11 (#412).

        Operator identity is synthesised from a fixed system-tenant
        sentinel because :class:`Connector.execute`'s ABC signature
        carries no operator. The synthesised operator's ``raw_jwt`` is
        the empty string — the shim's only callers are operator-less
        (``vault_readiness_probe`` exercises ``vault.sys.health``, an
        *unauthenticated* Vault endpoint that forwards no token),
        mirroring the
        :func:`~meho_backplane.topology.scheduler._system_operator`
        precedent. Callers that need a real operator JWT forwarded to
        Vault construct a real :class:`Operator` and call
        :func:`dispatch` directly (they never route through this shim).
        The synthesised ``sub`` and ``tenant_id`` only surface on the
        dispatcher's own audit row; routed call sites already write a
        richer audit row via
        :class:`~meho_backplane.audit.AuditMiddleware`, which carries
        the real operator identity from the JWT — the dispatcher's
        synthesised row is a strict subset.

        Newer callers
        (:func:`~meho_backplane.api.v1.health._probe_vault_federation`,
        the ``/api/v1/operations/call_operation`` meta-tool, the
        MCP ``call_operation`` handler) construct a real
        :class:`Operator` and invoke :func:`dispatch` directly; they
        do **not** route through this shim. The shim exists so
        callers that predate the G0.6 substrate keep working without
        edits — once every caller migrates, the shim can be deleted
        and :class:`Connector.execute` becomes abstract-only.

        The dispatcher's ``connector_id`` is ``"vault-1.x"`` — the
        canonical encoding of this connector's ``(product, version,
        impl_id)`` per the
        :func:`~meho_backplane.operations._lookup.parse_connector_id`
        contract.
        """
        # Lazy import: meho_backplane.operations.dispatch transitively
        # imports the connector registry which imports this module at
        # package import time; deferring the import until call time
        # keeps that initialisation order stable.
        from meho_backplane.operations import dispatch

        operator = self._synthesise_legacy_operator()
        return await dispatch(
            operator=operator,
            connector_id=self._dispatcher_connector_id(),
            op_id=op_id,
            target=target,
            params=params,
        )

    @classmethod
    def _dispatcher_connector_id(cls) -> str:
        """Encode this connector's natural key as the dispatcher's
        ``connector_id`` string.

        ``parse_connector_id`` splits ``"vault-1.x"`` into
        ``(product="vault", version="1.x", impl_id="vault")``; we
        encode the same shape on the call side so the dispatcher's
        natural-key lookup hits the row this connector's
        :func:`~meho_backplane.connectors.vault.ops.register_vault_typed_operations`
        registered.
        """
        if cls.version:
            return f"{cls.impl_id}-{cls.version}"
        # v1-style fallback. The connector ships with version="1.x"
        # so this branch is unreachable today; kept for the case
        # where a future subclass overrides version to "" to opt
        # back into v1 semantics.
        return cls.product

    @staticmethod
    def _synthesise_legacy_operator() -> Operator:
        """Build a minimal :class:`Operator` for the shim's dispatch call.

        The shim has no access to a real operator's identity claims —
        :class:`Connector.execute`'s ABC signature carries no operator.
        We synthesise:

        * ``sub`` — a fixed system sentinel
          (:data:`_SYSTEM_OPERATOR_SUB`) so the dispatcher's audit row
          for shimmed calls is greppable.
        * ``tenant_id`` — Nil UUID
          (:data:`_SYSTEM_TENANT_ID`). Typed registrations are always
          ``tenant_id IS NULL`` so the dispatcher's tenant-scoped
          descriptor lookup falls through to the global row regardless;
          the synthesised tenant_id only lands on the audit row.
        * ``raw_jwt`` — the empty string. The shim's only callers are
          operator-less and exercise the unauthenticated
          ``vault.sys.health`` op, which forwards no token to Vault
          (same contract as
          :func:`~meho_backplane.topology.scheduler._system_operator`).
          Authenticated ops require a real operator and call
          :func:`dispatch` directly, never through this shim.
        * ``tenant_role`` — :attr:`TenantRole.OPERATOR`. The v0.2
          policy gate doesn't read the role for ``safety_level='safe'``
          ops; the value is a placeholder that satisfies the model's
          required field.
        """
        return Operator(
            sub=_SYSTEM_OPERATOR_SUB,
            name=None,
            email=None,
            raw_jwt="",
            tenant_id=_SYSTEM_TENANT_ID,
            tenant_role=TenantRole.OPERATOR,
        )
