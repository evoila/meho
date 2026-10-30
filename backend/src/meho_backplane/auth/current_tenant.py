# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Pick the current tenant of a request (#5239).

One login can list several tenants (the ``meho_tenants`` claim, read into
:attr:`~meho_backplane.auth.operator.Operator.tenants` by #5236). MEHO
still works in **one current tenant** per request. This module picks it,
from the person's list and the ``Meho-Tenant`` header (a tenant UUID or a
tenant name). REST, MCP and the web interface all reach it through
:func:`~meho_backplane.auth.jwt.verify_jwt_for_audience`.

The picked tenant's values go into ``Operator.tenant_id``,
``tenant_role`` and ``approver``. Every check that reads those fields
(the role checks, the cross-tenant check, the Vault role, the Vault path
guard, the audit tenant) so keeps working, and sees only the current
tenant. Design: ``docs/decisions/2026-10-28-several-tenants-per-person.md``
section 2 and 3.

The rules:

==================  ============  =====================  =======================
The token lists     No header     Header names a tenant  Header names another
                                  in the list            tenant
==================  ============  =====================  =======================
no list (today)     its tenant    its tenant (UUID)      403 tenant_not_member
one tenant          that tenant   that tenant            403 tenant_not_member
several tenants     400           that tenant            403 tenant_not_member
                    tenant_not_
                    chosen
==================  ============  =====================  =======================

A platform admin (a person, never a machine) may also name any tenant that
exists in the ``tenant`` table. There they act as ``tenant_admin``, never
with ``approver``, and :attr:`Operator.role_from_platform_admin` is set so
the audit row can say so. A tenant that does not exist gives
``403 tenant_not_found``; the lookup never creates a tenant row. The
lookup never raises the role of a tenant the person is already in: when
the header names one of their own tenants by a name the token does not
carry (a token without a list holds only the UUID), they keep the role
of their group there.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

import structlog
from fastapi import HTTPException, status
from sqlalchemy import select

from meho_backplane.auth.operator import (
    Operator,
    TenantMembership,
    TenantRole,
    group_grant,
    is_human_principal,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Tenant

__all__ = [
    "TENANT_HEADER",
    "bind_tenant_role_source",
    "reject_duplicate_tenants",
    "select_current_tenant",
    "tenant_not_chosen_detail",
    "tenant_role_source_payload",
    "with_membership",
]

#: The request header that names the current tenant. Same style as
#: ``Meho-Work-Ref``.
TENANT_HEADER = "Meho-Tenant"

#: A header value longer than this cannot be a tenant UUID or name. It is
#: refused without a database lookup.
_MAX_HEADER_LENGTH = 200


def with_membership(operator: Operator, membership: TenantMembership) -> Operator:
    """Return *operator* working in the tenant of *membership*.

    The role and the ``approver`` flag come from the login group of that
    entry (:func:`~meho_backplane.auth.operator.group_grant`), never from
    another tenant.
    """
    grant = group_grant(membership.group)
    return operator.model_copy(
        update={
            "tenant_id": membership.tenant_id,
            "tenant_role": grant.role,
            "approver": grant.approver,
            "tenant_chosen": True,
            "role_from_platform_admin": False,
        }
    )


#: Payload key that says where the role in the current tenant came from.
TENANT_ROLE_SOURCE: Final[str] = "tenant_role_source"

#: The contextvar :func:`bind_tenant_role_source` binds: the payload key
#: under the ``audit_*`` prefix the wrapper writers read.
_TENANT_ROLE_SOURCE_CONTEXTVAR: Final[str] = f"audit_{TENANT_ROLE_SOURCE}"

#: The value when the role came from the platform admin group.
_PLATFORM_ADMIN_SOURCE: Final[str] = "platform_admin"


def bind_tenant_role_source(operator: Operator) -> None:
    """Note on the audit rows when the role came from the platform admin group.

    A platform admin working in a tenant without a role group there acts
    as ``tenant_admin``. Every audit row of the request then carries
    ``tenant_role_source: platform_admin`` in its payload (the ``audit_*``
    contextvar convention; no schema change). The wrapper writers read the
    contextvar. The audit cutover listener
    (:mod:`meho_backplane.audit_cutover`) copies the key onto every other
    row built while it is bound (:func:`tenant_role_source_from_context`),
    so a writer that composes its own payload needs no change. Nothing is
    bound otherwise. Called by the REST and MCP bind steps.
    """
    if operator.role_from_platform_admin:
        structlog.contextvars.bind_contextvars(
            **{_TENANT_ROLE_SOURCE_CONTEXTVAR: _PLATFORM_ADMIN_SOURCE}
        )


def tenant_role_source_payload(operator: Operator) -> dict[str, str]:
    """The payload keys an audit writer adds for the role source.

    ``{"tenant_role_source": "platform_admin"}`` when the role came from
    the platform admin group, else empty. For the audit writers that have
    the operator at hand and build their own payload (approval decisions,
    grants, topology edits, agent tool calls): the marker is then on the
    row also outside a request, for example in a test. With the wrapper
    rows retired, their row is the only record of the request.
    """
    if operator.role_from_platform_admin:
        return {TENANT_ROLE_SOURCE: _PLATFORM_ADMIN_SOURCE}
    return {}


def tenant_role_source_from_context() -> dict[str, str]:
    """The payload keys for the role source of the current request.

    Reads what :func:`bind_tenant_role_source` bound. Empty when the role
    did not come from the platform admin group, or outside a request. For
    the audit writers that know only the caller's ``sub`` (connector
    lifecycle actions, the runbook abort row, the dispatcher).
    """
    value = structlog.contextvars.get_contextvars().get(_TENANT_ROLE_SOURCE_CONTEXTVAR)
    if isinstance(value, str) and value:
        return {TENANT_ROLE_SOURCE: value}
    return {}


def reject_duplicate_tenants(tenants: tuple[TenantMembership, ...]) -> None:
    """Refuse a login with two role groups in the same tenant.

    One role group per tenant: with two, the role in that tenant is not
    clear. Raises ``401 multiple_role_groups`` and names the groups, as
    the old one-group check did (#4594). Groups of two different tenants
    are fine. The entries come from the trusted issuer's signed token, so
    they are safe to log and to send back.
    """
    by_tenant: dict[UUID, list[TenantMembership]] = {}
    for entry in tenants:
        by_tenant.setdefault(entry.tenant_id, []).append(entry)
    doubled = [entries for entries in by_tenant.values() if len(entries) > 1]
    if not doubled:
        return
    groups = [f"{entry.name}:{entry.group.value}" for entries in doubled for entry in entries]
    structlog.get_logger(__name__).warning("multiple_role_groups", groups=groups)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=(
            f"multiple_role_groups: this login is in more than one MEHO role group "
            f"of the same tenant ({', '.join(groups)}). A person must be in one role "
            "group per tenant. Ask your admin to keep you in one group of that tenant."
        ),
    )


def _tenant_names(operator: Operator) -> str:
    return ", ".join(entry.name for entry in operator.tenants)


def tenant_not_chosen_detail(operator: Operator) -> str:
    """The ``tenant_not_chosen`` message: the person's tenants and how to pick one."""
    return (
        f"tenant_not_chosen: this login is in {len(operator.tenants)} tenants "
        f"({_tenant_names(operator)}). Choose one with `meho tenants use <name>`, "
        f"or send the {TENANT_HEADER} header with a tenant name or ID."
    )


def _not_member(operator: Operator, wanted: str) -> HTTPException:
    structlog.get_logger(__name__).warning(
        "tenant_not_member",
        operator_sub=operator.sub,
        principal_kind=operator.principal_kind.value,
        requested_tenant=wanted[:_MAX_HEADER_LENGTH],
    )
    if operator.tenants:
        yours = f"Your tenants: {_tenant_names(operator)}."
    else:
        yours = f"This login works only in its own tenant {operator.tenant_id}."
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=(
            f"tenant_not_member: this login is not in the tenant named by the "
            f"{TENANT_HEADER} header. {yours}"
        ),
    )


def _parse_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None


def _match_entry(tenants: tuple[TenantMembership, ...], wanted: str) -> TenantMembership | None:
    """Find the entry *wanted* names, by tenant UUID or by tenant name."""
    wanted_id = _parse_uuid(wanted)
    wanted_name = wanted.lower()
    for entry in tenants:
        if entry.tenant_id == wanted_id or entry.name == wanted_name:
            return entry
    return None


async def _find_existing_tenant(wanted: str) -> UUID | None:
    """Return the id of the tenant row *wanted* names (UUID or slug), else ``None``.

    Read only: it never creates a row, so a typo in the header cannot make
    a new tenant.
    """
    wanted_id = _parse_uuid(wanted)
    condition = Tenant.id == wanted_id if wanted_id is not None else Tenant.slug == wanted
    async with get_sessionmaker()() as session:
        return (await session.execute(select(Tenant.id).where(condition))).scalar_one_or_none()


def _own_tenant(operator: Operator, tenant_id: UUID) -> Operator | None:
    """Return *operator* in *tenant_id* when it is one of their own tenants, else ``None``.

    The header may name a tenant by a name the token does not carry: a
    token without a list holds only the UUID, and a listed entry may use
    another name than the tenant row. The role of the person's own group
    there wins over the platform admin role, so a platform admin cannot
    raise their own role in a tenant by naming it differently.
    """
    own = _match_entry(operator.tenants, str(tenant_id))
    if own is not None:
        return with_membership(operator, own)
    if not operator.tenants and tenant_id == operator.tenant_id:
        return operator
    return None


async def _as_platform_admin(operator: Operator, wanted: str) -> Operator:
    """Let a platform admin work in an existing tenant that is not in their list."""
    tenant_id = await _find_existing_tenant(wanted)
    if tenant_id is None:
        structlog.get_logger(__name__).warning(
            "tenant_not_found",
            operator_sub=operator.sub,
            requested_tenant=wanted[:_MAX_HEADER_LENGTH],
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"tenant_not_found: no tenant matches the {TENANT_HEADER} header. "
                "Check the tenant name or ID. MEHO never creates a tenant from this header."
            ),
        )
    own = _own_tenant(operator, tenant_id)
    if own is not None:
        return own
    structlog.get_logger(__name__).info(
        "platform_admin_tenant_selected",
        operator_sub=operator.sub,
        tenant_id=str(tenant_id),
    )
    return operator.model_copy(
        update={
            "tenant_id": tenant_id,
            "tenant_role": TenantRole.TENANT_ADMIN,
            "approver": False,
            "tenant_chosen": True,
            "role_from_platform_admin": True,
        }
    )


async def select_current_tenant(
    operator: Operator,
    requested: str | None,
    *,
    allow_unchosen: bool = False,
) -> Operator:
    """Return *operator* working in its current tenant, by the rules above.

    *operator* comes from the token. With a tenant list, it already holds
    the values of the first entry; this function replaces them with the
    values of the tenant the request names.

    *allow_unchosen* is for the status answers only (``meho status``, the
    MCP tool ``meho_status`` and the MCP connection start). With it, a
    login with several tenants and no header is not refused; the result
    has ``tenant_chosen=False`` and must not be used for any tenant work.

    Raises 400 ``tenant_not_chosen``, 403 ``tenant_not_member`` or 403
    ``tenant_not_found``.
    """
    wanted = (requested or "").strip()
    if not wanted:
        if len(operator.tenants) <= 1:
            return operator
        if allow_unchosen:
            return operator.model_copy(update={"tenant_chosen": False})
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=tenant_not_chosen_detail(operator),
        )
    if len(wanted) > _MAX_HEADER_LENGTH:
        raise _not_member(operator, wanted)
    entry = _match_entry(operator.tenants, wanted)
    if entry is not None:
        return with_membership(operator, entry)
    if not operator.tenants and _parse_uuid(wanted) == operator.tenant_id:
        return operator
    if operator.platform_admin and is_human_principal(operator):
        return await _as_platform_admin(operator, wanted)
    raise _not_member(operator, wanted)
