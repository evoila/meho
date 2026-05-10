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
2. The handler enters
   :func:`~meho_backplane.auth.vault.vault_client_for_operator`, which
   forwards the *same* validated JWT to Vault's JWT/OIDC auth method.
   Vault verifies the JWT against its own configured trust of Keycloak
   (via the ``meho-mcp`` role) and issues a Vault token bound to the
   operator's identity.
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

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.vault import (
    VaultClientError,
    vault_client_for_operator,
)
from meho_backplane.middleware import verify_jwt_and_bind

__all__ = ["router"]

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

    ``reachable`` is true when ``vault_client_for_operator`` returns a
    logged-in client (TCP + TLS + JWT/OIDC login all worked). ``read_ok``
    is true when the test secret read succeeded against that client.
    ``detail`` carries a short structured string for the CLI to render
    on failure paths — never an unbounded exception message.
    """

    model_config = ConfigDict(frozen=True)

    reachable: bool
    read_ok: bool
    detail: str | None = None


class DbStatus(BaseModel):
    """Database migration status.

    ``migrated`` is ``None`` in v0.1 — G2.3 wires the real Alembic check.
    The field is present in the response shape now so the CLI's
    ``meho status`` rendering doesn't have to special-case its absence
    when G2.3 lands.
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


def _extract_version(secret_payload: Any) -> str:
    """Pull the KV v2 metadata version from a ``read_secret_version`` payload.

    hvac's KV v2 read returns ``{"data": {"data": ..., "metadata":
    {"version": int, ...}}}``. Accessing the version through nested
    ``[]`` calls inside a try/except would obscure the read-OK
    contract; this helper isolates the unwrap so the route handler stays
    legible. Any structural surprise (Vault returns an unexpected shape,
    metadata absent) raises ``KeyError`` / ``TypeError`` from the dict
    indexing, which the caller maps to ``read_ok=False`` with a
    ``read_failed`` detail.
    """
    data = secret_payload["data"]
    metadata = data["metadata"]
    version = metadata["version"]
    return f"version={version}"


async def _probe_vault_federation(
    operator: Operator,
    log: Any,
) -> VaultStatus:
    """Run the federation-proof Vault chain and return its structured status.

    Mirrors the route handler's three failure axes — login failure,
    secret-read failure, full success — onto a single :class:`VaultStatus`
    value. Lifted into a helper so the route handler stays focused on
    response assembly and the function-size budget stays under the
    code-quality threshold.

    Detail strings carry only exception class names; operator-controllable
    URL substrings never leak into a 200 response body or into a
    structlog payload.
    """
    try:
        async with vault_client_for_operator(operator) as client:
            try:
                # hvac's secrets.kv.v2 lives on the synchronous client; the
                # read is a blocking GET against Vault. Offload it to a
                # worker thread so the event loop stays free, mirroring
                # auth/vault.py's _to_thread_jwt_login pattern.
                secret_payload = await asyncio.to_thread(
                    client.secrets.kv.v2.read_secret_version,
                    path=_FEDERATION_PROOF_PATH,
                    raise_on_deleted_version=False,
                )
                # Unwrap inside the same try so a malformed hvac payload
                # (missing data/metadata/version keys) surfaces as a
                # structured read failure rather than escaping as an
                # HTTP 500 — AC #3 forbids 5xx on this endpoint. The
                # tight (KeyError, TypeError) catch is sufficient for the
                # dict-indexing failure modes _extract_version can raise;
                # the broader ``except Exception`` below covers hvac's own
                # error surface (Forbidden, InvalidPath, RequestException).
                detail = _extract_version(secret_payload)
            except (KeyError, TypeError) as exc:
                log.warning(
                    "federation_health_read_failed",
                    vault_read_path=_FEDERATION_PROOF_PATH,
                    exc_type=type(exc).__name__,
                )
                return VaultStatus(
                    reachable=True,
                    read_ok=False,
                    detail=f"read_failed: {type(exc).__name__}",
                )
            except Exception as exc:
                log.warning(
                    "federation_health_read_failed",
                    vault_read_path=_FEDERATION_PROOF_PATH,
                    exc_type=type(exc).__name__,
                )
                return VaultStatus(
                    reachable=True,
                    read_ok=False,
                    detail=f"read_failed: {type(exc).__name__}",
                )
            log.info("federation_health_ok", vault_read_path=_FEDERATION_PROOF_PATH)
            return VaultStatus(
                reachable=True,
                read_ok=True,
                detail=detail,
            )
    except VaultClientError as exc:
        log.warning("federation_health_login_failed", exc_type=type(exc).__name__)
        return VaultStatus(
            reachable=False,
            read_ok=False,
            detail=f"login_failed: {type(exc).__name__}",
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
    log = structlog.get_logger()
    vault_status = await _probe_vault_federation(operator, log)
    return HealthResponse(
        operator=OperatorIdentity(
            sub=operator.sub,
            name=operator.name,
            email=operator.email,
        ),
        vault=vault_status,
        db=DbStatus(migrated=None),
    )
