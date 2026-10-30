# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The current tenant of a web session (#5240).

The ``web_session`` row stores the tenant the person works in. Every
token check of the web interface passes it to the shared tenant choice
(:func:`~meho_backplane.auth.current_tenant.select_current_tenant`), the
same rule REST and MCP use with the ``Meho-Tenant`` header. So a role
check, a target list or an approval list sees only the session's tenant.

This module holds what the web interface adds on top:

* :func:`session_tenant_request` -- what the token check asks for. A
  session of a person with several tenants that has not picked one yet
  (``tenant_chosen=False``) asks for nothing; the web interface then
  shows only the tenant picker.
* :func:`can_switch_tenant` -- whether the sidebar shows the switch.
* :func:`save_session_tenant` -- write the tenant choice on the row.
* :func:`fall_back_after_removal` -- the token no longer allows the
  session's tenant (the person was taken out of it, or a platform admin
  lost the role). The session goes back to the picker, or to the only
  tenant left, and the request ends with :class:`TenantRemovedError`.

Design: ``docs/decisions/2026-10-28-several-tenants-per-person.md``
section 2, "Web interface".
"""

from __future__ import annotations

import uuid
from typing import Final, NoReturn

import structlog
from fastapi import HTTPException, status
from sqlalchemy import update

from meho_backplane.auth.jwt import verify_jwt_for_audience
from meho_backplane.auth.operator import Operator, is_human_principal
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import WebSession
from meho_backplane.ui.auth.session_store import DecryptedSession

__all__ = [
    "PAGE_TENANT_FORM_FIELD",
    "TENANT_CHANGED_HEADER",
    "TENANT_CHANGED_MESSAGE",
    "TENANT_PICKER_PATH",
    "TENANT_REMOVED_DETAIL",
    "TENANT_REMOVED_NOTICE",
    "TENANT_SWITCH_PATH",
    "TenantRemovedError",
    "can_switch_tenant",
    "fall_back_after_removal",
    "save_session_tenant",
    "session_tenant_request",
]

#: The tenant picker page. A session without a chosen tenant may open
#: only this page and :data:`TENANT_SWITCH_PATH`.
TENANT_PICKER_PATH: Final[str] = "/ui/tenant"

#: The form target that switches the session's tenant.
TENANT_SWITCH_PATH: Final[str] = "/ui/tenant/switch"

#: Query value on the picker link after the session lost its tenant.
TENANT_REMOVED_NOTICE: Final[str] = "removed"

#: ``detail`` of :class:`TenantRemovedError`.
TENANT_REMOVED_DETAIL: Final[str] = "tenant_removed"

#: Hidden form field with the tenant a page was made for. htmx requests
#: send the same value in the ``Meho-Tenant`` header.
PAGE_TENANT_FORM_FIELD: Final[str] = "meho_tenant"

#: Response header on the refusal of a request from a page made for
#: another tenant. ``session-expiry.js`` shows the reload banner on it.
TENANT_CHANGED_HEADER: Final[str] = "x-meho-tenant-changed"

#: The refusal text for a request from a page made for another tenant.
TENANT_CHANGED_MESSAGE: Final[str] = "The tenant changed. Reload the page."

#: The 403 codes of the shared tenant choice that mean "this token does
#: not allow the session's tenant any more".
_LOST_TENANT_CODES: Final[tuple[str, ...]] = ("tenant_not_member", "tenant_not_found")


class TenantRemovedError(HTTPException):
    """The session's tenant is no longer allowed; the row was already reset.

    The web interface answers with a redirect to the tenant picker
    (:mod:`meho_backplane.ui.auth.errors` for routes, the session
    middleware for page loads).
    """

    def __init__(self) -> None:
        super().__init__(status_code=status.HTTP_409_CONFLICT, detail=TENANT_REMOVED_DETAIL)


def session_tenant_request(decrypted: DecryptedSession) -> tuple[str | None, bool]:
    """Return ``(requested_tenant, allow_unchosen)`` for the session's token check."""
    if decrypted.tenant_chosen:
        return str(decrypted.tenant_id), False
    return None, True


def can_switch_tenant(operator: Operator) -> bool:
    """Return whether the person may pick another tenant in the web interface.

    True with more than one tenant in the token, or for a platform admin
    (a person, never a machine), who may pick any existing tenant.
    """
    return len(operator.tenants) > 1 or (operator.platform_admin and is_human_principal(operator))


def lost_tenant(exc: HTTPException) -> bool:
    """Return whether *exc* says the token does not allow the requested tenant."""
    return (
        exc.status_code == status.HTTP_403_FORBIDDEN
        and isinstance(exc.detail, str)
        and exc.detail.startswith(_LOST_TENANT_CODES)
    )


async def save_session_tenant(
    session_id: uuid.UUID,
    *,
    tenant_id: uuid.UUID,
    tenant_chosen: bool,
    can_switch: bool,
) -> None:
    """Write the tenant choice on the ``web_session`` row (own transaction)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as db_session, db_session.begin():
        await db_session.execute(
            update(WebSession)
            .where(WebSession.id == session_id)
            .values(
                tenant_id=tenant_id,
                tenant_chosen=tenant_chosen,
                can_switch_tenant=can_switch,
            )
        )


async def fall_back_after_removal(
    decrypted: DecryptedSession,
    *,
    access_token: str,
    expected_audience: str,
) -> NoReturn:
    """Reset a session whose tenant the token no longer allows, then raise.

    The token is checked again without a tenant. With one tenant left
    (or a token without a list) the session moves there. With several,
    the session goes back to the picker. Raises
    :class:`TenantRemovedError` in both cases, so nothing of the request
    runs in the old tenant.
    """
    operator = await verify_jwt_for_audience(
        f"Bearer {access_token}",
        expected_audience=expected_audience,
        allow_unchosen=True,
    )
    await save_session_tenant(
        decrypted.id,
        tenant_id=operator.tenant_id,
        tenant_chosen=operator.tenant_chosen,
        can_switch=can_switch_tenant(operator),
    )
    structlog.get_logger(__name__).info(
        "ui_session_tenant_removed",
        session_id=str(decrypted.id),
        operator_sub=decrypted.operator_sub,
        old_tenant_id=str(decrypted.tenant_id),
        new_tenant_id=str(operator.tenant_id) if operator.tenant_chosen else None,
    )
    raise TenantRemovedError()
