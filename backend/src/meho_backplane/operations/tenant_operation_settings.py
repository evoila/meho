# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Each tenant turns a built-in connector's operations on or off for itself (#5029).

A built-in operation row (``endpoint_descriptor.tenant_id IS NULL``) is
shared by every tenant, so its ``is_enabled`` flag is the **install
default**. This module stores one tenant's own choice next to it, in
:class:`~meho_backplane.db.models.TenantOperationSetting`:

* ``enabled=True`` -- the operation is on for this tenant, even when the
  install default is off.
* ``enabled=False`` -- the operation is off for this tenant, even when the
  install default is on.
* no row -- the tenant uses the install default.

Dispatch, ``search_operations``, ``list_operation_groups`` and the console
read the effective value through
:func:`~meho_backplane.operations._lookup.enabled_for_tenant_clause`.

Only built-in operations take a tenant setting. When the tenant has its own
copy of the connector (rows with its ``tenant_id``), those rows are the only
ones dispatch uses, so a setting would have no effect; the write is refused
with :class:`TenantOwnsConnectorError`.

Who may change a setting is decided by the REST route: a ``tenant_admin``
for their own tenant, a ``platform_admin`` for any tenant
(:func:`~meho_backplane.auth.rbac.authorize_tenant_scope`). The install
default stays the existing ``edit-op`` / ``enable`` paths, which need
``platform_admin`` for a built-in row.

Every change writes one audit row in the same transaction. A call that
changes nothing writes no row, like the other connector review actions.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationGroup, TenantOperationSetting
from meho_backplane.operations._lookup import parse_connector_id

__all__ = [
    "OP_TENANT_OPERATION_SETTING",
    "TenantOperationSettingItem",
    "TenantOperationSettingNotFoundError",
    "TenantOperationSettingsResponse",
    "TenantOwnsConnectorError",
    "list_operation_settings",
    "load_tenant_settings",
    "set_group_setting",
    "set_operation_setting",
]

#: Audit op id for a change of a tenant's own on/off setting.
OP_TENANT_OPERATION_SETTING: Final[str] = "meho_connector_tenant_setting"


class TenantOperationSettingNotFoundError(LookupError):
    """The connector, operation or group has no built-in row."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class TenantOwnsConnectorError(Exception):
    """The tenant has its own copy of the connector; its own rows are the switch."""

    def __init__(self, connector_id: str) -> None:
        self.message = (
            f"this tenant has its own copy of connector {connector_id!r}. "
            "Its own operations are turned on or off with `meho connector edit-op` "
            "(or enable / disable); a tenant setting applies only to built-in operations."
        )
        super().__init__(self.message)


class TenantOperationSettingItem(BaseModel):
    """One built-in operation with its install default and the tenant's own setting."""

    model_config = ConfigDict(frozen=True)

    op_id: str
    group_key: str | None
    default_enabled: bool
    tenant_enabled: bool | None
    enabled: bool


class TenantOperationSettingsResponse(BaseModel):
    """The tenant settings of one connector, for one tenant."""

    model_config = ConfigDict(frozen=True)

    connector_id: str
    tenant_id: uuid.UUID
    items: list[TenantOperationSettingItem]


def _item(
    op_id: str,
    group_key: str | None,
    default_enabled: bool,
    tenant_enabled: bool | None,
) -> TenantOperationSettingItem:
    return TenantOperationSettingItem(
        op_id=op_id,
        group_key=group_key,
        default_enabled=default_enabled,
        tenant_enabled=tenant_enabled,
        enabled=default_enabled if tenant_enabled is None else tenant_enabled,
    )


async def load_tenant_settings(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    product: str,
    version: str,
    impl_id: str,
) -> dict[str, bool]:
    """Return ``{op_id: enabled}`` for every setting *tenant_id* has on the connector."""
    rows = await session.execute(
        select(TenantOperationSetting.op_id, TenantOperationSetting.enabled).where(
            TenantOperationSetting.tenant_id == tenant_id,
            TenantOperationSetting.product == product,
            TenantOperationSetting.version == version,
            TenantOperationSetting.impl_id == impl_id,
        )
    )
    return dict(rows.tuples().all())


async def _resolve_builtin_connector(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
) -> tuple[str, str, str]:
    """Return the connector triple; refuse a tenant copy or a missing built-in."""
    product, version, impl_id = parse_connector_id(connector_id)
    same_connector = (
        EndpointDescriptor.product == product,
        EndpointDescriptor.version == version,
        EndpointDescriptor.impl_id == impl_id,
    )
    tenant_row = await session.execute(
        select(EndpointDescriptor.id)
        .where(EndpointDescriptor.tenant_id == tenant_id, *same_connector)
        .limit(1)
    )
    if tenant_row.first() is not None:
        raise TenantOwnsConnectorError(connector_id)
    builtin_row = await session.execute(
        select(EndpointDescriptor.id)
        .where(EndpointDescriptor.tenant_id.is_(None), *same_connector)
        .limit(1)
    )
    if builtin_row.first() is None:
        raise TenantOperationSettingNotFoundError(
            f"connector {connector_id!r} has no built-in operations"
        )
    return product, version, impl_id


async def _load_builtin_ops(
    session: AsyncSession,
    *,
    product: str,
    version: str,
    impl_id: str,
    op_ids: list[str] | None = None,
    group_id: uuid.UUID | None = None,
) -> list[tuple[str, str | None, bool]]:
    """Return ``(op_id, group_key, install default)`` for the matching built-in ops."""
    stmt = (
        select(EndpointDescriptor.op_id, OperationGroup.group_key, EndpointDescriptor.is_enabled)
        .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
        .where(
            EndpointDescriptor.tenant_id.is_(None),
            EndpointDescriptor.product == product,
            EndpointDescriptor.version == version,
            EndpointDescriptor.impl_id == impl_id,
        )
        .order_by(EndpointDescriptor.op_id)
    )
    if op_ids is not None:
        stmt = stmt.where(EndpointDescriptor.op_id.in_(op_ids))
    if group_id is not None:
        stmt = stmt.where(EndpointDescriptor.group_id == group_id)
    rows = await session.execute(stmt)
    return [(op_id, group_key, bool(is_enabled)) for op_id, group_key, is_enabled in rows.all()]


async def _apply(
    session: AsyncSession,
    *,
    operator: Operator,
    tenant_id: uuid.UUID,
    triple: tuple[str, str, str],
    op_ids: list[str],
    enabled: bool | None,
) -> list[str]:
    """Write *enabled* for every op in *op_ids*; return the op ids that changed."""
    product, version, impl_id = triple
    current = await load_tenant_settings(
        session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
    )
    changed = [op_id for op_id in op_ids if current.get(op_id) != enabled]
    if not changed:
        return []
    await session.execute(
        delete(TenantOperationSetting).where(
            TenantOperationSetting.tenant_id == tenant_id,
            TenantOperationSetting.product == product,
            TenantOperationSetting.version == version,
            TenantOperationSetting.impl_id == impl_id,
            TenantOperationSetting.op_id.in_(changed),
        )
    )
    if enabled is not None:
        now = datetime.now(UTC)
        session.add_all(
            TenantOperationSetting(
                tenant_id=tenant_id,
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                enabled=enabled,
                set_by_sub=operator.sub,
                set_at=now,
            )
            for op_id in changed
        )
    return changed


async def _audit(
    session: AsyncSession,
    *,
    operator: Operator,
    payload: dict[str, Any],
) -> None:
    # Imported here: the ingest package imports this module for the review
    # payload, so a top-level import would be circular.
    from meho_backplane.operations.ingest._internals import write_audit_row

    await write_audit_row(
        session,
        operator_sub=operator.sub,
        operator_tenant_id=operator.tenant_id,
        op_id=OP_TENANT_OPERATION_SETTING,
        payload=payload,
    )


async def set_operation_setting(
    operator: Operator,
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
    op_id: str,
    enabled: bool | None,
) -> TenantOperationSettingItem:
    """Set *tenant_id*'s own setting for one built-in operation.

    ``enabled=None`` removes the setting, so the tenant uses the install
    default again. The caller has already checked that *operator* may act
    on *tenant_id*. Returns the operation with both values.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        triple = await _resolve_builtin_connector(
            session, tenant_id=tenant_id, connector_id=connector_id
        )
        ops = await _load_builtin_ops(
            session, product=triple[0], version=triple[1], impl_id=triple[2], op_ids=[op_id]
        )
        if not ops:
            raise TenantOperationSettingNotFoundError(
                f"operation {op_id!r} not found on connector {connector_id!r}"
            )
        changed = await _apply(
            session,
            operator=operator,
            tenant_id=tenant_id,
            triple=triple,
            op_ids=[op_id],
            enabled=enabled,
        )
        if changed:
            await _audit(
                session,
                operator=operator,
                payload={
                    "connector_id": connector_id,
                    "target_tenant_id": str(tenant_id),
                    "op_id": op_id,
                    "tenant_enabled": enabled,
                },
            )
            await session.commit()
    _, group_key, default_enabled = ops[0]
    return _item(op_id, group_key, default_enabled, enabled)


async def set_group_setting(
    operator: Operator,
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
    group_key: str,
    enabled: bool | None,
) -> list[TenantOperationSettingItem]:
    """Set *tenant_id*'s own setting for every built-in operation in one group.

    A group setting is the same per-operation setting written for each
    operation in the group, the way the install-default group enable
    cascades onto each operation. ``enabled=None`` removes the settings.
    One audit row covers the whole group. Returns the group's operations.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        product, version, impl_id = await _resolve_builtin_connector(
            session, tenant_id=tenant_id, connector_id=connector_id
        )
        group_id = (
            await session.execute(
                select(OperationGroup.id).where(
                    OperationGroup.tenant_id.is_(None),
                    OperationGroup.product == product,
                    OperationGroup.version == version,
                    OperationGroup.impl_id == impl_id,
                    OperationGroup.group_key == group_key,
                )
            )
        ).scalar_one_or_none()
        if group_id is None:
            raise TenantOperationSettingNotFoundError(
                f"group {group_key!r} not found on connector {connector_id!r}"
            )
        ops = await _load_builtin_ops(
            session, product=product, version=version, impl_id=impl_id, group_id=group_id
        )
        changed = await _apply(
            session,
            operator=operator,
            tenant_id=tenant_id,
            triple=(product, version, impl_id),
            op_ids=[op_id for op_id, _, _ in ops],
            enabled=enabled,
        )
        if changed:
            await _audit(
                session,
                operator=operator,
                payload={
                    "connector_id": connector_id,
                    "target_tenant_id": str(tenant_id),
                    "group_key": group_key,
                    "tenant_enabled": enabled,
                    "ops_changed_count": len(changed),
                },
            )
            await session.commit()
    return [_item(op_id, key, default, enabled) for op_id, key, default in ops]


async def list_operation_settings(
    *,
    tenant_id: uuid.UUID,
    connector_id: str,
) -> TenantOperationSettingsResponse:
    """Return every built-in operation of the connector that *tenant_id* has a setting for.

    Operations without a setting use the install default; the review
    payload (``GET /api/v1/connectors/{id}/review``) shows those.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        product, version, impl_id = await _resolve_builtin_connector(
            session, tenant_id=tenant_id, connector_id=connector_id
        )
        settings = await load_tenant_settings(
            session, tenant_id=tenant_id, product=product, version=version, impl_id=impl_id
        )
        ops = (
            await _load_builtin_ops(
                session,
                product=product,
                version=version,
                impl_id=impl_id,
                op_ids=sorted(settings),
            )
            if settings
            else []
        )
    return TenantOperationSettingsResponse(
        connector_id=connector_id,
        tenant_id=tenant_id,
        items=[_item(op_id, key, default, settings[op_id]) for op_id, key, default in ops],
    )
