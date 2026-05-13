# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /api/v1/health`` — federation-proof authenticated health endpoint.

This route is the load-bearing integration point for Goal #11's smoke
test. A single call exercises the entire authn / authz / federation
chain end-to-end:

1. The :func:`~meho_backplane.middleware.verify_jwt_and_bind` dependency
   runs :func:`~meho_backplane.auth.jwt.verify_jwt` against the incoming
   ``Authorization: Bearer <jwt>`` header, validates the JWT against
   Keycloak's JWKS, and binds the resulting ``operator_sub`` into
   structlog's contextvars (so every log line under this request carries
   the operator's identity).
2. The handler dispatches to
   :class:`~meho_backplane.connectors.vault.VaultConnector` (via the
   connector registry), which forwards the *same* validated JWT to
   Vault's JWT/OIDC auth method. Vault verifies the JWT against its own
   configured trust of Keycloak (via the ``meho-mcp`` role) and issues a
   Vault token bound to the operator's identity.
3. The handler reads ``secret/meho/test/federation`` (KV v2) using the
   per-operator Vault token. The read is the **federation proof**: if
   Vault's audit log shows this operator's ``sub`` against the read,
   the entire chain is wired correctly.
4. The handler returns a structured JSON document carrying operator
   identity, Vault status, and a placeholder for DB migration state
   (which G2.3 will wire). The CLI's ``meho status`` command renders
   this for the operator.

Failure handling is **never** a 5xx. Vault unreachable, Vault role
denied, secret read failure — each surfaces as the corresponding flag
on the response with a structured ``detail`` string the smoke test can
read. The authentication failure modes (missing / expired / tampered
JWT) are the responsibility of :func:`verify_jwt` and remain 401s; that
is the only error class operators routinely chase against this endpoint.

Detail strings deliberately surface only exception class names, never
their messages, so a misconfigured Vault role can't leak operator-
controllable URL substrings into a successful 200 response.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors import get_connector
from meho_backplane.connectors.vault.connector import VaultConnector, VaultTarget
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["build_health_response", "router"]

#: Hardcoded path inside the Vault KV v2 mount used to prove the
#: federation chain. The path is provisioned by the consumer (see
#: Goal #11's "Cross-repo dependencies" — Vault role + KV mount
#: ``secret/meho/`` + this path under it). Per-route customization of
#: which secret to read is explicitly out of scope for v0.1; product
#: routes added post-Goal-2 will read different paths.
_FEDERATION_PROOF_PATH: str = "meho/test/federation"


class OperatorIdentity(BaseModel):
    """Operator identity surface exposed to the CLI.

    Excludes ``raw_jwt`` deliberately — the bearer token must never
    appear in a response body, and the :class:`Operator` model carries
    it for downstream Vault forward-auth only.
    """

    model_config = ConfigDict(frozen=True)

    sub: str
    name: str | None
    email: str | None


class VaultStatus(BaseModel):
    """Vault federation-chain status.

    ``reachable`` is true when the OIDC login succeeded (TCP + TLS +
    JWT forward all worked). ``read_ok`` is true when the test secret
    read succeeded against the resulting Vault token. ``detail`` carries
    a short structured string for the CLI to render on failure paths —
    never an unbounded exception message.
    """

    model_config = ConfigDict(frozen=True)

    reachable: bool
    read_ok: bool
    detail: str | None = None


class DbStatus(BaseModel):
    """Database migration status.

    ``migrated`` is ``True`` when the DB-migration-state probe reports
    healthy (current Alembic revision matches head), ``False`` when
    the probe reports unhealthy for any reason (DB unreachable,
    revision diverged, ``alembic_version`` table absent). v0.1 ships
    no opinions on retry / repair — operators see the probe's
    ``detail`` string on the ``/ready`` payload (and downstream tooling
    in T29 enforces the migration-runner contract). The field stays
    ``bool | None`` for forward compatibility with response decoders
    that were generated against the chassis-stage shape, but the
    handler now always populates it from the probe.
    """

    model_config = ConfigDict(frozen=True)

    migrated: bool | None


class HealthResponse(BaseModel):
    """``GET /api/v1/health`` response body."""

    model_config = ConfigDict(frozen=True)

    operator: OperatorIdentity
    vault: VaultStatus
    db: DbStatus


router = APIRouter(prefix="/api/v1", tags=["health"])


async def _probe_vault_federation(
    operator: Operator,
    log: Any,
) -> VaultStatus:
    """Run the federation-proof Vault chain via the connector registry.

    Dispatches to :class:`~meho_backplane.connectors.vault.VaultConnector`
    through :func:`~meho_backplane.connectors.get_connector` so the route
    no longer holds a direct dependency on the auth.vault implementation
    details. The three failure axes (login failure, read failure, full
    success) are conveyed through the ``OperationResult.extras["phase"]``
    and ``extras["exc_type"]`` fields.

    Detail strings carry only exception class names; operator-controllable
    URL substrings never leak into a 200 response body or into a structlog
    payload.
    """
    # Prefer the registry-dispatched class (populated by lifespan _eager_import_connectors
    # in production). Fall back to direct instantiation in test contexts where
    # the connector registry has been cleared by test isolation fixtures.
    vault_cls = get_connector("vault") or VaultConnector

    target = VaultTarget(raw_jwt=operator.raw_jwt)
    result = await vault_cls().execute(target, "vault.kv.read", {"path": _FEDERATION_PROOF_PATH})

    if result.status == "ok":
        version = result.extras.get("version")
        detail = f"version={version}" if version is not None else "ok"
        log.info("federation_health_ok", vault_read_path=_FEDERATION_PROOF_PATH)
        return VaultStatus(reachable=True, read_ok=True, detail=detail)

    phase = result.extras.get("phase", "read")
    exc_type = result.extras.get("exc_type", "unknown")

    if phase == "login":
        log.warning("federation_health_login_failed", exc_type=exc_type)
        return VaultStatus(reachable=False, read_ok=False, detail=f"login_failed: {exc_type}")

    log.warning(
        "federation_health_read_failed",
        vault_read_path=_FEDERATION_PROOF_PATH,
        exc_type=exc_type,
    )
    return VaultStatus(reachable=True, read_ok=False, detail=f"read_failed: {exc_type}")


async def build_health_response(operator: Operator) -> HealthResponse:
    """Assemble the :class:`HealthResponse` for a validated operator.

    Lifted out of :func:`authenticated_health` so the MCP reference tool
    ``meho.status`` (G0.5-T4) can return the same federation-proof status
    bundle without re-implementing the Vault + DB probe chain. The route
    handler is now a thin wrapper around this helper plus the
    :class:`Operator` dependency the FastAPI router supplies; the MCP
    tool handler at :mod:`meho_backplane.mcp.tools.meho_status` calls
    this directly with the :class:`Operator` the MCP dispatcher already
    resolved.
    """
    log = structlog.get_logger()
    vault_status = await _probe_vault_federation(operator, log)
    db_probe_result = await db_migration_probe()
    return HealthResponse(
        operator=OperatorIdentity(
            sub=operator.sub,
            name=operator.name,
            email=operator.email,
        ),
        vault=vault_status,
        db=DbStatus(migrated=db_probe_result.ok),
    )


@router.get("/health", response_model=HealthResponse)
async def authenticated_health(
    operator: Operator = Depends(verify_jwt_and_bind),
) -> HealthResponse:
    """Federation-proof authenticated health check.

    See module docstring for the four-step chain this exercises. The
    ``Depends(verify_jwt_and_bind)`` annotation is what guarantees the
    JWT is validated *and* ``operator_sub`` is bound into structlog
    contextvars before this body runs — every log line emitted from
    here downstream carries operator identity automatically.
    """
    return await build_health_response(operator)
