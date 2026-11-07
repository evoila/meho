# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Service functions for the per-tenant policy columns on ``tenant``.

The PATCH routes in :mod:`meho_backplane.api.v1.tenants` and the settings-file
import (:mod:`meho_backplane.instance_settings.service`) both write the same
columns, so the write lives here once. Each function:

* applies only the values that actually change on the loaded :class:`Tenant`;
* binds ``audit_*`` contextvars naming field / old / new, which
  :class:`~meho_backplane.audit.AuditMiddleware` folds into this request's
  ``audit_log`` row (governance config never changes silently);
* evicts the matching resolver cache, where there is one (flight recorder,
  announce gate), at once and again after the session commits
  (:func:`~meho_backplane.db.after_commit.clear_now_and_after_commit`), so the
  saved change governs the next dispatch without waiting out the 60s TTL. A
  read between the write and the commit cannot keep the old policy in the
  cache. The mail allowlist has no cache: it is read on every send.

Validation (bounds, allowlist grammar) stays with the caller's input model;
these functions trust their arguments.
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy.orm import object_session

from meho_backplane.broadcast.announce_gate import invalidate_announce_gate_cache
from meho_backplane.db.after_commit import clear_now_and_after_commit
from meho_backplane.db.models import Tenant
from meho_backplane.flight_recorder.config import invalidate_tenant_policy_cache

__all__ = [
    "FLIGHT_RECORDER_FIELDS",
    "INHERIT_SENTINEL",
    "set_announce_gate_enabled",
    "set_flight_recorder_policy",
    "set_mail_recipient_allowlist",
    "set_run_approval_target_kinds",
]

_log = structlog.get_logger(__name__)

#: The three ``tenant`` columns the flight-recorder policy owns.
FLIGHT_RECORDER_FIELDS: tuple[str, ...] = (
    "flight_recorder_enabled",
    "flight_recorder_agent_readable",
    "flight_recorder_retention_days",
)

#: Audit-payload sentinel for the tri-state fields' ``inherit`` (NULL) state.
#: :func:`~meho_backplane.audit._resolve_audit_payload` drops ``None``
#: contextvars, so a ``True``/``False`` -> ``NULL`` transition would silently
#: lose its "after" value; the sentinel keeps the transition legible in the
#: ``audit_log`` payload. Mirrors the ``""`` unpinned marker the target CA-pin
#: audit uses for the same reason.
INHERIT_SENTINEL = "inherit"


def _audit_value(value: object) -> object:
    """Render a policy value for the audit payload (``None`` -> sentinel)."""
    return INHERIT_SENTINEL if value is None else value


def set_flight_recorder_policy(tenant: Tenant, updates: dict[str, Any]) -> list[str]:
    """Apply flight-recorder policy *updates* to *tenant*; return the changed fields.

    *updates* maps a subset of :data:`FLIGHT_RECORDER_FIELDS` to new values
    (``None`` clears a nullable field back to inherit). On any change the
    per-tenant capture resolver cache is evicted, now and after the commit.
    """
    changed: list[str] = []
    for field, new_value in updates.items():
        if field not in FLIGHT_RECORDER_FIELDS:
            raise ValueError(f"not a flight-recorder policy field: {field}")
        old_value = getattr(tenant, field)
        if old_value == new_value:
            continue
        setattr(tenant, field, new_value)
        # The field name already carries the ``flight_recorder_`` prefix, so
        # the payload keys read ``flight_recorder_enabled_before`` etc. after
        # the middleware strips the ``audit_`` prefix.
        structlog.contextvars.bind_contextvars(
            **{
                f"audit_{field}_before": _audit_value(old_value),
                f"audit_{field}_after": _audit_value(new_value),
            }
        )
        changed.append(field)

    if changed:
        structlog.contextvars.bind_contextvars(
            audit_flight_recorder_policy_changed=True,
            audit_tenant_id=str(tenant.id),
        )
        tenant_id = tenant.id
        clear_now_and_after_commit(
            object_session(tenant), lambda: invalidate_tenant_policy_cache(tenant_id)
        )
        _log.info("tenant_flight_recorder_policy_updated", tenant_id=str(tenant.id), fields=changed)
    return changed


def set_mail_recipient_allowlist(tenant: Tenant, value: str | None) -> bool:
    """Set *tenant*'s mail-recipient allowlist; return whether it changed.

    ``None`` = inherit the instance floor, ``""`` = deny every send, a
    comma-separated string = the tenant's own recipient space. The mail
    resolver reads the row on every send (#5756), so there is no cache to evict.
    """
    old_value = tenant.mail_recipient_allowlist
    if old_value == value:
        return False
    tenant.mail_recipient_allowlist = value
    structlog.contextvars.bind_contextvars(
        audit_mail_recipient_allowlist_before=_audit_value(old_value),
        audit_mail_recipient_allowlist_after=_audit_value(value),
        audit_mail_recipient_policy_changed=True,
        audit_tenant_id=str(tenant.id),
    )
    _log.info("tenant_mail_recipient_policy_updated", tenant_id=str(tenant.id))
    return True


def set_announce_gate_enabled(tenant: Tenant, value: bool) -> bool:
    """Set *tenant*'s announce-gate flag (#3133); return whether it changed.

    There is no REST route for this column; the settings import is its only
    writer besides direct seeding.
    """
    old_value = tenant.announce_gate_enabled
    if old_value == value:
        return False
    tenant.announce_gate_enabled = value
    structlog.contextvars.bind_contextvars(
        audit_announce_gate_enabled_before=old_value,
        audit_announce_gate_enabled_after=value,
        audit_announce_gate_policy_changed=True,
        audit_tenant_id=str(tenant.id),
    )
    tenant_id = tenant.id
    clear_now_and_after_commit(
        object_session(tenant), lambda: invalidate_announce_gate_cache(tenant_id)
    )
    _log.info("tenant_announce_gate_updated", tenant_id=str(tenant.id), enabled=value)
    return True


def set_run_approval_target_kinds(tenant: Tenant, value: bool) -> bool:
    """Set *tenant*'s run approval target kind setting (#4126); return whether it changed.

    ``True`` lets a run approval list a target kind (a product) in place of
    one target. Read at call time with no cache, so nothing to evict. The
    settings import is its only writer besides direct seeding.
    """
    old_value = tenant.run_approval_target_kinds_enabled
    if old_value == value:
        return False
    tenant.run_approval_target_kinds_enabled = value
    structlog.contextvars.bind_contextvars(
        audit_run_approval_target_kinds_before=old_value,
        audit_run_approval_target_kinds_after=value,
        audit_tenant_id=str(tenant.id),
    )
    _log.info("tenant_run_approval_target_kinds_updated", tenant_id=str(tenant.id), enabled=value)
    return True
