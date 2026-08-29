# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``/api/v1/addons/pairings*`` — REST surface for the add-on pairing handshake (#3025).

Operator-facing lifecycle over
:class:`~meho_backplane.operations.addon_pairing.AddonPairingService`, the
foundation of Initiative #2900. Pairing registers a sibling add-on product
as a Keycloak client-credentials **service** principal bound to a negotiated
integration-contract version; unpairing is reversible and hard-deletes the
pairing, leaving the backplane byte-identical to a never-paired one. Every
mutation binds ``audit_op_id`` + ``audit_op_class`` so the synchronous,
append-only audit middleware records the pair / unpair.

Route inventory
---------------

* ``POST /api/v1/addons/pairings`` — pair an add-on. Body:
  :class:`PairAddonRequest`. 201 with a one-time
  :class:`PairAddonResult` (the freshly-minted ``client_secret``); 409 on a
  contract-version skew or a duplicate pairing; 422 on a bad name; 503/502
  when Keycloak admin is unconfigured / failing. Role: ``tenant_admin``.
* ``GET /api/v1/addons/pairings`` — list active pairings (``{items,
  next_cursor}`` envelope). Role: ``operator``.
* ``GET /api/v1/addons/pairings/{name}`` — fetch one pairing. 404 when
  absent / cross-tenant. Role: ``operator``.
* ``DELETE /api/v1/addons/pairings/{name}`` — unpair. 204 on success; 404
  when absent; 503/502 on Keycloak admin failure. Role: ``tenant_admin``.
* ``POST /api/v1/addons/pairings/{name}/heartbeat`` — the paired add-on
  reports liveness (stamps ``last_seen_at``). Requires a **service**
  principal (403 otherwise); 404 when the add-on is not paired.

Tenant scoping: every route derives ``tenant_id`` from the JWT-validated
:class:`~meho_backplane.auth.operator.Operator`; no surface accepts a tenant
id from the body or query string.
"""

from __future__ import annotations

from typing import Annotated, Final

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path, Query
from fastapi import status as http_status
from fastapi.responses import Response

from meho_backplane.auth.keycloak_admin import (
    KeycloakAdminError,
    KeycloakAdminNotConfiguredError,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.rbac import require_role
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.addon_pairing import (
    AddonAlreadyPairedError,
    AddonNotPairedError,
    AddonPairingService,
)
from meho_backplane.operations.addon_pairing_contract import ContractSkewError
from meho_backplane.operations.addon_pairing_schemas import (
    PairAddonRequest,
    PairAddonResult,
    PairedAddonListResponse,
    PairedAddonRead,
)
from meho_backplane.operations.addon_step_events import (
    AddonStepEventService,
    StepEventListResponse,
)

__all__ = ["router"]

router = APIRouter(prefix="/api/v1/addons/pairings", tags=["addon-pairing"])

#: Module-level Depends closures — avoid ruff B008 (mutable call in a
#: default-argument position). Pairing/unpairing is a tenant_admin action
#: (it mints / revokes an identity and its standing posture); listing is
#: operator-visible.
_require_operator = Depends(require_role(TenantRole.OPERATOR))
_require_admin = Depends(require_role(TenantRole.TENANT_ADMIN))
_require_jwt = Depends(verify_jwt_and_bind)

_OP_IDS: Final[dict[str, str]] = {
    "list": "addon.list",
    "show": "addon.show",
    "pair": "addon.pair",
    "unpair": "addon.unpair",
    "heartbeat": "addon.heartbeat",
    "events": "addon.events",
}

#: Default / maximum step-event page size for the subscription read.
_EVENTS_DEFAULT_LIMIT: Final[int] = 100
_EVENTS_MAX_LIMIT: Final[int] = 500


def _handle_admin_error(exc: KeycloakAdminError) -> HTTPException:
    """Map a Keycloak Admin API failure onto the boundary status code.

    ``KeycloakAdminNotConfiguredError`` -> 503 (the deploy has not wired the
    Keycloak admin client, so the pairing surface is unavailable, not
    broken); any other ``KeycloakAdminError`` -> 502 (Keycloak reachable but
    the Admin API call failed). Details carry only the exception class name,
    never a message, so no infra-topology string leaks.
    """
    if isinstance(exc, KeycloakAdminNotConfiguredError):
        return HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="keycloak_admin_not_configured",
        )
    return HTTPException(
        status_code=http_status.HTTP_502_BAD_GATEWAY,
        detail=f"keycloak_admin_error:{type(exc).__name__}",
    )


@router.post("", response_model=PairAddonResult, status_code=http_status.HTTP_201_CREATED)
async def pair_addon(
    payload: PairAddonRequest,
    operator: Operator = _require_admin,
) -> PairAddonResult:
    """Pair an add-on (the one-time handshake).

    Returns 201 with the one-time credentials on success; 409 on a
    contract-version skew (``detail`` names the direction) or a duplicate
    pairing; 422 on a malformed name; 503/502 when Keycloak admin is
    unconfigured / failing. Audited via the audit middleware.
    """
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["pair"],
        audit_op_class="write",
        audit_addon_name=payload.name,
    )
    service = AddonPairingService()
    try:
        return await service.pair(operator.tenant_id, operator.sub, payload)
    except ContractSkewError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=exc.code,
        ) from exc
    except AddonAlreadyPairedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="addon_already_paired",
        ) from exc
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


@router.get("", response_model=PairedAddonListResponse)
async def list_pairings(
    operator: Operator = _require_operator,
) -> PairedAddonListResponse:
    """List active pairings for the operator's tenant (tenant-scoped)."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["list"],
        audit_op_class="read",
    )
    service = AddonPairingService()
    items = await service.list_(operator.tenant_id)
    return PairedAddonListResponse(items=items, next_cursor=None)


@router.get("/{name}", response_model=PairedAddonRead)
async def show_pairing(
    name: Annotated[str, Path()],
    operator: Operator = _require_operator,
) -> PairedAddonRead:
    """Return one pairing by name. Cross-tenant probes return 404."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["show"],
        audit_op_class="read",
    )
    service = AddonPairingService()
    entry = await service.get(operator.tenant_id, name)
    if entry is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        )
    return entry


@router.delete("/{name}", status_code=http_status.HTTP_204_NO_CONTENT)
async def unpair_addon(
    name: Annotated[str, Path()],
    operator: Operator = _require_admin,
) -> Response:
    """Unpair an add-on (reversible). 204 on success; 404 when not paired."""
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["unpair"],
        audit_op_class="write",
        audit_addon_name=name,
    )
    service = AddonPairingService()
    try:
        unpaired = await service.unpair(operator.tenant_id, name)
    except KeycloakAdminError as exc:
        raise _handle_admin_error(exc) from exc
    if not unpaired:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post("/{name}/heartbeat", response_model=PairedAddonRead)
async def heartbeat_pairing(
    name: Annotated[str, Path()],
    operator: Operator = _require_jwt,
) -> PairedAddonRead:
    """Record an add-on liveness heartbeat (paired service principal only).

    The paired add-on authenticates as its own service principal and stamps
    ``last_seen_at``. A non-service principal is 403; an unpaired add-on is
    404. Audited via the audit middleware.
    """
    if operator.principal_kind is not PrincipalKind.SERVICE:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="heartbeat_requires_service_principal",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["heartbeat"],
        audit_op_class="write",
        audit_addon_name=name,
    )
    service = AddonPairingService()
    try:
        return await service.heartbeat(operator.tenant_id, name)
    except AddonNotPairedError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        ) from exc


@router.get("/{name}/events", response_model=StepEventListResponse)
async def list_step_events(
    name: Annotated[str, Path()],
    after: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=_EVENTS_MAX_LIMIT)] = _EVENTS_DEFAULT_LIMIT,
    operator: Operator = _require_jwt,
) -> StepEventListResponse:
    """Durable, resumable step-event subscription for the paired add-on (#3027).

    The add-on authenticates as its own **service** principal (a non-service
    principal is 403) and reads the step events (approval outcomes, dispatch
    completions) that belong to **its own** work. Scoping is cryptographic:
    the caller is bound to a pairing by its token ``sub`` (which is the
    pairing's ``service_account_sub``), and ``{name}`` must be that same
    pairing — a service principal can never read another add-on's log. A
    caller whose ``sub`` binds to no pairing, or whose bound pairing is not
    ``{name}``, gets 404 (indistinguishable from a missing pairing).

    Resume: ``after`` is the last ``seq`` the add-on saw (``0`` reads from
    the start of the retained log); the response's ``next_cursor`` is the
    ``seq`` to pass as ``after`` on the next poll, so an add-on never misses
    a committed event across its own restarts.
    """
    if operator.principal_kind is not PrincipalKind.SERVICE:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="events_require_service_principal",
        )
    structlog.contextvars.bind_contextvars(
        audit_op_id=_OP_IDS["events"],
        audit_op_class="read",
        audit_addon_name=name,
    )
    service = AddonStepEventService()
    pairing = await service.resolve_pairing_for_sub(
        tenant_id=operator.tenant_id,
        service_account_sub=operator.sub,
    )
    # A 404 whether the caller binds to no pairing or to a different one:
    # never reveal another add-on's pairing name, and never leak whether a
    # name exists to a principal that does not own it.
    if pairing is None or pairing.name != name:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="addon_not_paired",
        )
    return await service.list_for_pairing(
        pairing_id=pairing.id,
        after_seq=after,
        limit=limit,
    )
