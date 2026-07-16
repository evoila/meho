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

Backend-agnostic federation proof (#2231)
=========================================

The four-step chain above describes the Vault deployment — the default
and, until #2230, the only credential backend. The proof is now
dispatched on ``config.credentialBackend`` / ``CREDENTIAL_BACKEND``: a
Vault install takes the unchanged ``vault.kv.read`` path
(:func:`_probe_vault_federation`, zero migration); any other backend
(``gsm`` on a GCP-native install) reads its designated probe secret
through the credential-backend seam (:func:`_probe_backend_federation`).
Either way the response shape is identical — the ``vault`` field carries
whichever backend's federation status — so the CLI ``meho status`` and
the ``meho.status`` MCP tool render a GSM install exactly as they render
a Vault one. The ``gsm`` probe reads under MEHO's own deployment identity
(SA-direct, #2230), so its probe secret is reachable without the
per-operator Vault tenant-scope exemption in
:data:`~meho_backplane.connectors.vault.tenant_scope.PLATFORM_EXEMPT_PATHS`
(that guard is on the Vault op path only).
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.connectors._shared.credential_backend import (
    UnknownCredentialBackendError,
    resolve_credential_backend,
    split_credential_ref,
)
from meho_backplane.db.migrations import db_migration_probe
from meho_backplane.mcp.schemas import PROTOCOL_VERSION
from meho_backplane.mcp.server import mcp_session_id_capture_mode
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations import dispatch
from meho_backplane.settings import get_settings

__all__ = ["build_health_response", "router"]

#: Vault backend kind — the schemeless default (``config.credentialBackend`` /
#: ``CREDENTIAL_BACKEND`` default). When the deployment runs on Vault the
#: federation proof takes the unchanged :func:`_probe_vault_federation`
#: dispatch path; any other backend routes through the credential-backend
#: seam (:func:`_probe_backend_federation`).
_VAULT_BACKEND_KIND: str = "vault"

#: Hardcoded path inside the Vault KV v2 mount used to prove the
#: federation chain. The path is provisioned by the consumer (see
#: Goal #11's "Cross-repo dependencies" — Vault role + KV mount
#: ``secret/meho/`` + this path under it). Per-route customization of
#: which secret to read is explicitly out of scope for v0.1; product
#: routes added post-Goal-2 will read different paths.
_FEDERATION_PROOF_PATH: str = "meho/test/federation"

#: GCP Secret Manager secret name the GSM-backend federation proof reads
#: under ``Settings.gsm_project`` (``config.gsmProject`` / ``GSM_PROJECT``).
#: The GSM analogue of :data:`_FEDERATION_PROOF_PATH` — hyphenated because
#: Secret Manager secret ids are ``[A-Za-z0-9_-]`` (no ``/``). A GSM-only
#: install provisions ``projects/<gsm_project>/secrets/meho-test-federation``
#: with a JSON-object value (e.g. ``{"ok": "true"}``) so the seam read
#: returns a field dict, exactly as the Vault probe reads a KV-v2 secret.
_FEDERATION_PROOF_GSM_SECRET: str = "meho-test-federation"

#: Target name threaded into the credential-backend seam for the probe read;
#: it never names a credential value, only labels the read in error/log
#: strings (the backend contract's ``target_name``).
_FEDERATION_PROOF_TARGET_NAME: str = "health-federation-proof"


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
    """Federation-chain status for the deployment's credential backend.

    The field is named ``vault`` on :class:`HealthResponse` for wire
    compatibility (it predates the credential-backend seam), but the value
    reflects whichever backend ``config.credentialBackend`` selects: the
    Vault OIDC-login + KV read on a Vault install, or the configured
    backend's probe-secret read (``gsm:<project>/<probe-secret>`` on a GSM
    install) through the credential-backend seam.

    ``reachable`` is true when the backend was reached — on Vault, the OIDC
    login succeeded (TCP + TLS + JWT forward); on a seam backend, the store
    was addressable (config resolved, backend registered). ``read_ok`` is
    true when the probe secret read succeeded. ``detail`` carries a short
    structured string for the CLI to render on failure paths — a class name
    or error code, never an unbounded exception message or a secret value.
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


def _federation_probe_ref(backend_kind: str) -> str | None:
    """Build the scheme-prefixed probe ref for a non-Vault *backend_kind*.

    Returns a ``<kind>:<store-ref>`` value the credential-backend seam
    resolves, or ``None`` when the backend has no probe convention or is
    not configured enough to address one (e.g. ``gsm`` without
    ``GSM_PROJECT`` set). ``None`` maps to a ``config_error`` status rather
    than a spurious store read against an empty project.

    * ``gsm`` → ``gsm:<gsm_project>/meho-test-federation`` (latest version),
      or ``None`` when ``Settings.gsm_project`` is unset.
    """
    if backend_kind == "gsm":
        project = get_settings().gsm_project.strip()
        if not project:
            return None
        return f"gsm:{project}/{_FEDERATION_PROOF_GSM_SECRET}"
    return None


async def _probe_backend_federation(
    operator: Operator,
    log: Any,
    backend_kind: str,
) -> VaultStatus:
    """Run the federation proof through the credential-backend seam.

    The non-Vault path (``config.credentialBackend != "vault"``): resolve
    the backend registered for *backend_kind* on the #2229 seam and read
    its designated probe secret. Success / failure map onto the same
    :class:`VaultStatus` axes the Vault dispatch path uses, so the
    ``/api/v1/health`` response shape is identical regardless of backend.

    The read forwards *operator* to satisfy the seam contract; a
    deployment-identity backend (GSM SA-direct, #2230) reads under MEHO's
    own identity and ignores it, so the GSM probe secret is reachable
    without a per-operator tenant-scope exemption (the Vault tenant-scope
    guard is not on this path — see
    :data:`~meho_backplane.connectors.vault.tenant_scope.PLATFORM_EXEMPT_PATHS`).

    Never raises: the module's never-a-5xx contract holds by mapping every
    failure axis (unconfigured probe, unknown backend kind, store read
    error) to a :class:`VaultStatus` with ``read_ok=False`` and a class-
    name / error-code ``detail`` that never echoes a secret value.
    """
    ref = _federation_probe_ref(backend_kind)
    if ref is None:
        log.warning("federation_health_probe_unconfigured", backend=backend_kind)
        return VaultStatus(
            reachable=False,
            read_ok=False,
            detail=f"config_error: {backend_kind}",
        )

    kind, store_ref = split_credential_ref(ref, default_backend=backend_kind)
    try:
        backend = resolve_credential_backend(kind)
    except UnknownCredentialBackendError:
        log.warning("federation_health_unknown_backend", backend=kind)
        return VaultStatus(reachable=False, read_ok=False, detail=f"unknown_backend: {kind}")

    try:
        await backend.load_secret_data(
            store_ref,
            operator,
            target_name=_FEDERATION_PROOF_TARGET_NAME,
            mount="",
        )
    except Exception as exc:
        # Never-a-5xx contract: any backend read failure (missing secret,
        # denied access, ADC error, malformed payload) surfaces as
        # read_ok=False with the exception class name, exactly as the Vault
        # dispatch path renders its read-phase failures. Only the class name
        # is surfaced — never the message, so operator-controllable
        # substrings can't leak into a 200 response body.
        exc_type = type(exc).__name__
        log.warning("federation_health_backend_read_failed", backend=kind, exc_type=exc_type)
        return VaultStatus(reachable=True, read_ok=False, detail=f"read_failed: {exc_type}")

    log.info("federation_health_ok", backend=kind)
    return VaultStatus(reachable=True, read_ok=True, detail="ok")


async def _probe_federation(operator: Operator, log: Any) -> VaultStatus:
    """Dispatch the federation proof to the deployment's credential backend.

    On Vault (the default, ``config.credentialBackend == "vault"``) this is
    the unchanged :func:`_probe_vault_federation` dispatch path — same
    ``vault.kv.read`` op, same audit / policy-gate / broadcast, zero
    migration. Any other backend routes through
    :func:`_probe_backend_federation` over the credential-backend seam.

    ``_probe_vault_federation`` is resolved as a module global so existing
    tests that monkeypatch it keep working through this dispatcher.
    """
    backend_kind = get_settings().credential_backend
    if backend_kind == _VAULT_BACKEND_KIND:
        return await _probe_vault_federation(operator, log)
    return await _probe_backend_federation(operator, log, backend_kind)


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
    vault_status = await _probe_federation(operator, log)
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
