# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""VaultConnector — HashiCorp Vault connector reference implementation.

This module is the proof step for the G0.2 abstraction (Initiative #223).
The OIDC-login-then-read pattern already implemented in
:mod:`meho_backplane.auth.vault` is reused through module-level references
so the existing test seams (``vault_module._build_client`` monkeypatches)
remain valid without duplication.

Auth model: ``shared_service_account`` — every operator's JWT is
forwarded to Vault's JWT/OIDC auth method bound to the ``meho-mcp`` role.
The resulting Vault token is per-request and revoked on context exit
(behaviour inherited from :func:`~meho_backplane.auth.vault.vault_client_for_operator`).

Target contract (pre-G0.3 placeholder):

:class:`VaultTarget` is a minimal stand-in for the ``Target`` model that
G0.3 (#224) will land. Once G0.3 merges, replace ``VaultTarget`` usages
with proper ``Target`` instances. The connector reads Vault connection
parameters (address, role, mount path, namespace, timeout) from
:func:`~meho_backplane.settings.get_settings` rather than from the target
because those are deployment-level settings, not per-operator overrides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import hvac.exceptions
import requests.exceptions
import structlog

import meho_backplane.auth.vault as _auth_vault
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.connectors.vault.ops import OP_MAP
from meho_backplane.settings import get_settings

__all__ = ["VaultConnector", "VaultTarget"]

_log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class VaultTarget:
    """Pre-G0.3 stand-in for the G0.3 Target model (VaultConnector only).

    Replace with ``meho_backplane.targets.Target`` once G0.3 (#224) lands.
    Only ``raw_jwt`` is needed today; connection parameters are read from
    :func:`~meho_backplane.settings.get_settings` so the same single-source
    of truth governs probe, fingerprint, and execute.
    """

    raw_jwt: str | None = None


class VaultConnector(Connector):
    """HashiCorp Vault connector — shared_service_account auth via OIDC."""

    product = "vault"

    async def fingerprint(self, target: VaultTarget) -> FingerprintResult:
        """Canonical fingerprint from ``GET /v1/sys/health``.

        Reuses :func:`~meho_backplane.auth.vault._build_client` so the
        existing test seam applies and the vault_timeout / namespace
        settings are respected without duplicating the client-construction
        logic.
        """
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

    async def probe(self, target: VaultTarget) -> ProbeResult:
        """Lightweight reachability check via unauthenticated ``/v1/sys/health``.

        Reuses :func:`~meho_backplane.auth.vault._build_client` so the test
        seam (``monkeypatch.setattr(vault_module, "_build_client", fake)``)
        applies to this method as well, keeping the existing
        ``test_auth_vault.py`` suite green after ``vault_readiness_probe``
        is refactored to delegate here.

        The ``reason`` field carries the same detail strings that
        :func:`~meho_backplane.auth.vault.vault_readiness_probe` previously
        embedded directly (``"sealed=False"``, ``"sealed"``,
        ``"uninitialized"``, ``"http_429"``, etc.) — callers that need the
        old ``detail`` shape map from ``reason``.
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
        target: VaultTarget,
        op_id: str,
        params: dict[str, Any],
    ) -> OperationResult:
        """Dispatch to the per-op handler in :data:`~meho_backplane.connectors.vault.ops.OP_MAP`.

        Unknown op-ids return a structured error with ``known_ops`` in
        extras so callers can enumerate what the connector supports without
        inspecting source code.
        """
        handler = OP_MAP.get(op_id)
        if handler is None:
            return OperationResult(
                status="error",
                op_id=op_id,
                error=f"unknown_op: {op_id}",
                duration_ms=0.0,
                extras={"known_ops": list(OP_MAP.keys())},
            )
        return await handler(target, params)
