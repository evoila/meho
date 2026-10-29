# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which subscriptions a wider Azure read may cover: the subscription set.

A wider read (for example a Resource Graph query) covers more than the
subscription of the calling target. Its ``subscriptions`` parameter names
other registered ``azure`` targets (one target is one subscription). Before
any call to Azure, :func:`resolve_subscription_set` checks each name:

* it is a live ``azure`` target of the caller's MEHO tenant, and a usable one;
* it uses the same login (secret reference) and the same Azure cloud as the
  calling target, because one Azure call runs with one login;
* the caller may run this operation on it now. MEHO asks the dispatcher's own
  permission check (``policy_gate``) for that target, and records nothing.
  A target where the call would wait for approval is denied too: a read over
  several subscriptions never waits for approval.

The calling target is always in the set; the dispatcher has already checked
it. Azure then checks its own roles. So what the stored login could read never
widens what the caller gets.

The read reports the outcome in one block, ``subscriptions: {requested,
completed, denied, failed}``; ``denied`` and ``failed`` give a reason per
name. :meth:`SubscriptionSet.bind_audit` puts the same block into the audit
row of the call (the dispatcher copies every ``audit_*`` value bound in the
log context into the row's ``payload``).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast
from uuid import UUID

import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.connectors._shared.vcf_auth import is_acceptable_auth_model
from meho_backplane.connectors.azure.session import (
    AzureTargetConfig,
    AzureTargetConfigError,
    AzureTargetLike,
    resolve_target_config,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PermissionVerdict
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate

if TYPE_CHECKING:
    from meho_backplane.connectors.azure.connector import AzureConnector

__all__ = [
    "MAX_SUBSCRIPTION_NAMES",
    "SUBSCRIPTIONS_BLOCK_SCHEMA",
    "SUBSCRIPTIONS_PARAMETER",
    "SetMember",
    "SubscriptionSet",
    "resolve_subscription_set",
]

#: Most target names one wider read may name.
MAX_SUBSCRIPTION_NAMES: Final[int] = 1000

#: The key the block is bound under in the log context. The dispatcher strips
#: ``audit_`` and writes the value to ``payload["subscriptions"]``.
_AUDIT_KEY: Final[str] = "audit_subscriptions"

#: The ``subscriptions`` parameter of every wider read.
SUBSCRIPTIONS_PARAMETER: Final[dict[str, Any]] = {
    "type": "array",
    "maxItems": MAX_SUBSCRIPTION_NAMES,
    "items": {"type": "string", "minLength": 1, "maxLength": 200},
    "description": (
        "Names of other registered azure targets (one target is one subscription) "
        "to read as well. The calling target is always read. Each target must use "
        "the same login (secret reference) as the calling target, and you must be "
        "allowed to run this operation on it; any other name is listed under "
        "subscriptions.denied with the reason. Leave it out to read the calling "
        "target only."
    ),
}

_NAME_LIST: Final[dict[str, Any]] = {"type": "array", "items": {"type": "string"}}
_REASON_LIST: Final[dict[str, Any]] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "The target name."},
            "reason": {"type": "string", "description": "Why, in plain words."},
        },
    },
}

#: The ``subscriptions`` block in the result of every wider read.
SUBSCRIPTIONS_BLOCK_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "description": "Which subscriptions (target names) the read covered.",
    "properties": {
        "requested": {
            **_NAME_LIST,
            "description": "Every name asked for, the calling target first.",
        },
        "completed": {**_NAME_LIST, "description": "The names MEHO read in full."},
        "denied": {
            **_REASON_LIST,
            "description": "Names MEHO did not read because of a check, each with the reason.",
        },
        "failed": {
            **_REASON_LIST,
            "description": "Names MEHO could not read in full, each with the reason.",
        },
    },
}


@dataclass(frozen=True, slots=True)
class SetMember:
    """One subscription the read may cover: the target name and its subscription ID."""

    name: str
    subscription_id: str


@dataclass
class SubscriptionSet:
    """The checked set: what was asked for, what may be read, and the outcome."""

    requested: list[str]
    members: list[SetMember]
    denied: list[dict[str, str]] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    def mark_completed(self, members: Iterable[SetMember]) -> None:
        """Record that *members* were read in full."""
        self.completed.extend(member.name for member in members)

    def mark_failed(self, members: Iterable[SetMember], reason: str) -> None:
        """Record that *members* could not be read in full, and why."""
        self.failed.extend({"name": member.name, "reason": reason} for member in members)

    def as_block(self) -> dict[str, Any]:
        """The ``subscriptions`` block of the result (a copy)."""
        return {
            "requested": list(self.requested),
            "completed": list(self.completed),
            "denied": [dict(entry) for entry in self.denied],
            "failed": [dict(entry) for entry in self.failed],
        }

    def bind_audit(self) -> None:
        """Put the block into the audit row of the current call."""
        structlog.contextvars.bind_contextvars(**{_AUDIT_KEY: self.as_block()})


async def resolve_subscription_set(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    *,
    op_id: str,
    names: Sequence[str],
) -> SubscriptionSet:
    """Check every name in *names* for a wider read of *op_id* from *target*.

    Returns the set with the calling target first. A name that fails a check
    is not an error: it is listed under ``denied`` with the reason, and the
    read goes on without it. Raises only when the calling target itself is not
    a usable Azure target. The block is bound for the audit row at once, so a
    read that fails later still records which names were checked.
    """
    # A block from an earlier call in the same context must not reach this row.
    structlog.contextvars.unbind_contextvars(_AUDIT_KEY)
    config = resolve_target_config(target)
    others = list(dict.fromkeys(name for name in names if name != target.name))
    subscription_set = SubscriptionSet(
        requested=[target.name, *others],
        members=[SetMember(target.name, config.subscription_id)],
    )
    if others:
        await _check_others(connector, operator, target, config, op_id, others, subscription_set)
    subscription_set.bind_audit()
    return subscription_set


async def _check_others(
    connector: AzureConnector,
    operator: Operator,
    target: AzureTargetLike,
    config: AzureTargetConfig,
    op_id: str,
    others: list[str],
    subscription_set: SubscriptionSet,
) -> None:
    """Add each name in *others* to the set's members or to ``denied``."""
    rows = await _live_targets(operator.tenant_id, others)
    descriptor = await lookup_descriptor(
        tenant_id=operator.tenant_id,
        product=connector.product,
        version=connector.version,
        impl_id=connector.impl_id,
        op_id=op_id,
    )
    connector_id = f"{connector.impl_id}-{connector.version}"
    for name in others:
        other = rows.get(name)
        other_config, reason = _shape_check(other, target, config, connector.product)
        if reason is None and other is not None:
            reason = await _permission_check(operator, descriptor, other, connector_id, op_id)
        if reason is not None or other_config is None:
            subscription_set.denied.append({"name": name, "reason": reason or "Denied."})
            continue
        subscription_set.members.append(SetMember(name, other_config.subscription_id))


async def _live_targets(tenant_id: UUID, names: list[str]) -> dict[str, TargetORM]:
    """The live targets of *tenant_id* with one of *names*, by name."""
    async with get_sessionmaker()() as session:
        result = await session.execute(
            select(TargetORM).where(
                TargetORM.tenant_id == tenant_id,
                TargetORM.name.in_(names),
                TargetORM.deleted_at.is_(None),
            )
        )
        return {row.name: row for row in result.scalars()}


def _shape_check(
    other: TargetORM | None,
    target: AzureTargetLike,
    config: AzureTargetConfig,
    product: str,
) -> tuple[AzureTargetConfig | None, str | None]:
    """Check that *other* can be read with the login of *target*.

    Returns the other target's settings and ``None``, or ``None`` and the
    reason it is denied.
    """
    if other is None:
        return None, "No live target with this name in your MEHO tenant."
    if other.product != product:
        return None, f"This target is not an {product} target."
    try:
        # The row has the target's fields; mypy only sees narrower column types.
        other_config = resolve_target_config(cast("AzureTargetLike", other))
    except AzureTargetConfigError as exc:
        return None, f"This target is not a usable Azure target: {exc}"
    if not is_acceptable_auth_model(other.auth_model):
        return None, "This target does not use the shared login (auth_model)."
    if not other.secret_ref or other.secret_ref != target.secret_ref:
        return None, (
            "This target uses another login (secret reference). One query runs with "
            "one login: the one of the calling target."
        )
    if other_config.cloud.name != config.cloud.name:
        return None, "This target is in another Azure cloud."
    return other_config, None


async def _permission_check(
    operator: Operator,
    descriptor: EndpointDescriptor | None,
    other: TargetORM,
    connector_id: str,
    op_id: str,
) -> str | None:
    """Ask the dispatcher's permission check for *other*. ``None`` means allowed."""
    if descriptor is None:
        return f"MEHO could not find the settings of {op_id}, so it could not check the permission."
    verdict, reason = await policy_gate(
        operator=operator,
        descriptor=descriptor,
        target=other,
        connector_id=connector_id,
        decide_only=True,
    )
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        return None
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        return (
            f"{op_id} would wait for approval on this target. A read over several "
            "subscriptions never waits for approval: run it on this target alone."
        )
    detail = f" ({reason})" if reason else ""
    return f"You may not run {op_id} on this target{detail}."
