# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Authenticated health endpoints — liveness probe + federation-proof deep check.

Two routes with deliberately different privilege requirements:

* ``GET /api/v1/health/live`` — a **cheap liveness probe** for
  low-privilege / monitoring principals. Requires only a valid JWT
  (any tenant role, including ``read_only``) and reports operator
  identity plus DB-migration liveness. Its code path never touches
  Vault: no connector call, no per-operator credential federation,
  no secret read.
* ``GET /api/v1/health`` — the **federation-proof deep check**, gated
  at :data:`~meho_backplane.auth.operator.TenantRole.OPERATOR` via
  :func:`~meho_backplane.auth.rbac.require_role`. Every call drives a
  live Vault JWT/OIDC login plus a KV v2 secret read under the
  caller's identity, so least privilege demands the gate: a
  ``read_only`` principal receives 403 ``insufficient_role`` before
  any Vault credential is federated. Low-privilege monitoring callers
  that only need "is the process up?" use ``/health/live`` instead.

The deep check is the load-bearing integration point for Goal #11's
smoke test. A single call exercises the entire authn / authz /
federation chain end-to-end:

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

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from meho_backplane.mcp.server import mcp_session_id_capture_mode
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import dispatch

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
    """``GET /api/v1/health`` response body.

    ``mcp_session_id_capture`` (G0.14-T6 #1147) reports the deploy's
    audit-replay capture mode in a single field:

    * ``"always"`` — any ``Mcp-Session-Id`` header the client sends is
      captured into ``audit_log.agent_session_id``; a missing header is
      accepted (the row's session id lands as NULL). This is the
      default and what G8.2 audit-replay needs to light up on a stock
      deploy.
    * ``"enforced"`` — capture works the same way **plus** a missing
      header is a JSON-RPC ``-32600`` reject before any audit row is
      written. Flipped on by ``MCP_REQUIRE_SESSION_ID=true`` in
      compliance deploys that forbid header-less calls.

    The field is the canonical operator-facing surface for the
    capture state until T7 #1148's ``/ready`` features block ships
    the richer enumeration (T7's ``audit_replay`` block pulls from
    the same
    :func:`~meho_backplane.mcp.server.mcp_session_id_capture_mode`
    helper so both surfaces stay consistent).

    ``mcp_protocol_version`` (G0.14-T13 #1202) reports the server's
    pinned :data:`~meho_backplane.mcp.schemas.PROTOCOL_VERSION`.
    Mirrors the ``mcp_session_id_capture`` precedent: single-field
    operator visibility into the MCP layer's runtime state. The
    matching ``mcp.protocol_version`` entry on ``/ready``'s features
    block (see :func:`meho_backplane.features.build_features_block`)
    surfaces the same value on the unauthenticated readiness probe so
    a deploy operator can answer "which MCP revision will this server
    negotiate with my clients?" without an authenticated GET.
    """

    model_config = ConfigDict(frozen=True)

    operator: OperatorIdentity
    vault: VaultStatus
    db: DbStatus
    mcp_session_id_capture: str
    mcp_protocol_version: str = PROTOCOL_VERSION


class LivenessResponse(BaseModel):
    """``GET /api/v1/health/live`` response body.

    The cheap, low-privilege liveness surface: operator identity (from
    the already-validated JWT — zero extra cost) plus DB-migration
    liveness from
    :func:`~meho_backplane.db.migrations.db_migration_probe`. No Vault
    field on purpose — this model must never grow one. The federation
    proof lives exclusively on the OPERATOR-gated
    :class:`HealthResponse` route so a low-privilege monitoring
    principal can never drive a per-operator Vault credential
    federation from the liveness path.
    """

    model_config = ConfigDict(frozen=True)

    operator: OperatorIdentity
    db: DbStatus


router = APIRouter(prefix="/api/v1", tags=["health"])

#: Module-level dependency singleton (sibling-router convention, and
#: what keeps ruff B008 happy): resolves the OPERATOR rank once at
#: import time so a typo'd role fails at boot, not per-request.
_require_operator = Depends(require_role(TenantRole.OPERATOR))


#: Vault exception class names that indicate a login-phase failure (the
#: OIDC forward to Vault failed before any secret read attempted). The
#: dispatcher's ``connector_error`` branch reports the raised exception's
#: class name verbatim in ``extras["exception_class"]``; we string-match
#: by name rather than by ``issubclass`` so the health route avoids a
#: hard import of ``meho_backplane.auth.vault`` at module top-level
#: (which would pull the hvac client into every health probe import).
#:
#: ``VaultClientError`` covers any future subclass added under it; the
#: two named subclasses are listed explicitly so the rendered ``detail``
#: string is stable across hvac upgrades that swap which subclass fires.
_VAULT_LOGIN_PHASE_EXCEPTION_CLASSES: frozenset[str] = frozenset(
    {
        "VaultClientError",
        "VaultUnreachableError",
        "VaultRoleDeniedError",
    }
)


async def _probe_vault_federation(
    operator: Operator,
    log: Any,
) -> VaultStatus:
    """Run the federation-proof Vault chain via the G0.6 dispatcher.

    Dispatches the ``vault.kv.read`` typed op through
    :func:`~meho_backplane.operations.dispatch` so the route inherits the
    substrate's parameter validation, policy gate, audit-log write,
    broadcast publish, and JSONFlux wrapping in one place. The
    connector's :func:`~meho_backplane.connectors.vault.ops.vault_kv_read`
    handler is module-level and is invoked by the dispatcher's typed
    branch directly — :class:`VaultConnector`'s ``execute`` shim is **not**
    in this call chain.

    The three failure axes (login failure, read failure, full success)
    are conveyed through:

    * ``result.status == "ok"`` -> full success; ``result.result``
      carries ``{"data": <secret>, "version": <int|None>}``.
    * ``result.status == "error"`` + ``error_code == "connector_error"``
      with ``exception_class`` in :data:`_VAULT_LOGIN_PHASE_EXCEPTION_CLASSES`
      -> login-phase failure (Vault unreachable, role denied, or any
      future :class:`VaultClientError` subclass).
    * Any other error shape -> read-phase failure (KV miss, malformed
      hvac payload, jsonschema validation miss, etc.).

    Detail strings carry only exception class names; operator-
    controllable URL substrings never leak into a 200 response body
    or into a structlog payload.
    """
    # The vault.kv.read handler reads the operator JWT from the
    # request-scoped Operator (G0.8-T3 #629), not a target row; the
    # connector is resolved by connector_id, so target is None.
    result = await dispatch(
        operator=operator,
        connector_id="vault-1.x",
        op_id="vault.kv.read",
        target=None,
        params={"path": _FEDERATION_PROOF_PATH},
    )

    if result.status == "ok":
        # The handler returns ``{"data": <secret>, "version": <int|None>}``;
        # the dispatcher's default reducer passes this small scalar dict
        # through as ``result.result`` unchanged.
        payload = result.result if isinstance(result.result, dict) else {}
        version = payload.get("version")
        detail = f"version={version}" if version is not None else "ok"
        log.info("federation_health_ok", vault_read_path=_FEDERATION_PROOF_PATH)
        return VaultStatus(reachable=True, read_ok=True, detail=detail)

    exc_type = result.extras.get("exception_class")
    if not isinstance(exc_type, str):
        # Non-connector_error shapes (``unknown_op``, ``invalid_params``,
        # ``no_connector``, ``denied``) don't carry an
        # ``exception_class`` -- map them to the read-phase failure
        # path so the smoke test's "never a 5xx" contract holds. The
        # ``error_code`` value lands in the detail string so operators
        # see the dispatcher-side classification on the CLI without
        # parsing the audit log.
        error_code = result.extras.get("error_code", "unknown")
        log.warning(
            "federation_health_dispatch_failed",
            vault_read_path=_FEDERATION_PROOF_PATH,
            error_code=error_code,
            error_message=result.error,
        )
        return VaultStatus(
            reachable=True,
            read_ok=False,
            detail=f"read_failed: {error_code}",
        )

    if exc_type in _VAULT_LOGIN_PHASE_EXCEPTION_CLASSES:
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
        mcp_session_id_capture=mcp_session_id_capture_mode(),
        mcp_protocol_version=PROTOCOL_VERSION,
    )


@router.get("/health/live", response_model=LivenessResponse)
async def liveness(
    operator: Operator = Depends(verify_jwt_and_bind),
) -> LivenessResponse:
    """Cheap liveness probe for low-privilege / monitoring callers.

    Requires a valid JWT (``verify_jwt_and_bind`` — 401 on missing /
    invalid tokens, and ``operator_sub`` lands in structlog
    contextvars) but deliberately carries **no role gate**: the lowest
    tenant role (``read_only``) held by monitoring principals must be
    able to poll it. In exchange, this handler is forbidden from
    touching Vault — it must never invoke a connector, federate a
    per-operator Vault credential, or read a secret. The federation
    proof belongs to :func:`authenticated_health` behind the OPERATOR
    gate.
    """
    db_probe_result = await db_migration_probe()
    return LivenessResponse(
        operator=OperatorIdentity(
            sub=operator.sub,
            name=operator.name,
            email=operator.email,
        ),
        db=DbStatus(migrated=db_probe_result.ok),
    )


@router.get("/health", response_model=HealthResponse)
async def authenticated_health(
    operator: Operator = _require_operator,
) -> HealthResponse:
    """Federation-proof authenticated health check (OPERATOR-gated).

    See module docstring for the four-step chain this exercises. The
    ``Depends(require_role(TenantRole.OPERATOR))`` dependency wraps
    ``verify_jwt_and_bind`` — the JWT is validated *and*
    ``operator_sub`` is bound into structlog contextvars before the
    role check runs — and then enforces least privilege: every call
    here federates a live per-operator Vault credential and reads a
    KV secret, which is not a ``read_only``-rank capability. Callers
    below OPERATOR receive 403 ``insufficient_role`` before any Vault
    interaction; monitoring principals poll ``/health/live`` instead.
    """
    return await build_health_response(operator)
