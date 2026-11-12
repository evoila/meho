# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Load and change the stored classification of operations.

Two places hold classification entries:

* :class:`~meho_backplane.db.models.TenantPolicy`: one document per
  tenant and one for the whole install, with the taxonomy and the
  pattern entries for connectors, groups and operations;
* :class:`~meho_backplane.db.models.OperationPolicyOverride`: the
  ``category`` and ``labels`` of one operation, an exact operation entry.

:func:`classify_rows` reads both and returns the classified values of
each operation row (:func:`~meho_backplane.policy.classification.classify`).
:func:`set_classification` changes one entry of a tenant's document. It
runs only after a second admin approved the change (the
``policy.classify`` operation, :mod:`meho_backplane.connectors.policy`).

Nothing here runs while the ``POLICY_CLASSIFICATION`` switch is off
(:func:`classification_enabled`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from sqlalchemy import ColumnElement, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationGroup,
    OperationPolicyOverride,
    TenantPolicy,
)
from meho_backplane.policy.classification import (
    Classification,
    ClassificationEntry,
    OpCoordinates,
    Scope,
    Taxonomy,
    check_no_conflict,
    classify,
    entries_from_document,
    taxonomy_from_documents,
)
from meho_backplane.settings import get_settings

__all__ = [
    "SWITCH_OFF_REFUSAL",
    "ClassificationEdit",
    "EditPlan",
    "InvalidStoredPolicyError",
    "classification_enabled",
    "classify_rows",
    "classify_step",
    "connector_id_of",
    "load_classification",
    "plan_edit",
    "set_classification",
]

_MAX_LABELS = 32
_MAX_LABEL_KEY = 63
_MAX_LABEL_VALUE = 255


class InvalidStoredPolicyError(ValueError):
    """A stored policy document is not valid, so no operation can be classified."""


#: What every surface says while the switch is off.
SWITCH_OFF_REFUSAL: Final[str] = (
    "Classification is off on this MEHO. An admin turns it on with the "
    "POLICY_CLASSIFICATION setting."
)


def classification_enabled() -> bool:
    """True when the ``POLICY_CLASSIFICATION`` switch is on."""
    return get_settings().policy_classification


def connector_id_of(row: EndpointDescriptor) -> str:
    """The wire connector id of *row*: ``<impl_id>-<version>``."""
    return f"{row.impl_id}-{row.version}"


async def _documents(
    session: AsyncSession, tenant_id: UUID | None
) -> tuple[TenantPolicy | None, TenantPolicy | None]:
    """Return the install's and the tenant's stored policy rows (each may be absent)."""
    scope: ColumnElement[bool] = TenantPolicy.tenant_id.is_(None)
    if tenant_id is not None:
        scope = or_(scope, TenantPolicy.tenant_id == tenant_id)
    install: TenantPolicy | None = None
    tenant: TenantPolicy | None = None
    for row in (await session.execute(select(TenantPolicy).where(scope))).scalars():
        if row.tenant_id is None:
            install = row
        else:
            tenant = row
    return install, tenant


def _override_entries(
    overrides: Sequence[OperationPolicyOverride], connector_id: str
) -> list[ClassificationEntry]:
    """The ``category`` and ``labels`` of override rows, as exact operation entries."""
    entries: list[ClassificationEntry] = []
    for override in overrides:
        labels = override.labels if isinstance(override.labels, dict) else {}
        if override.category is None and not labels:
            continue
        scope: Scope = "install" if override.tenant_id is None else "tenant"
        entries.append(
            ClassificationEntry(
                ref=f"{scope}:override",
                scope=scope,
                connector=connector_id,
                op=override.op_id,
                category=override.category,
                labels={str(k): str(v) for k, v in labels.items()},
            )
        )
    return entries


async def load_classification(
    session: AsyncSession, tenant_id: UUID | None
) -> tuple[Taxonomy, list[ClassificationEntry]]:
    """Return the taxonomy and the pattern entries that apply to *tenant_id*."""
    install, tenant = await _documents(session, tenant_id)
    install_doc = install.document if install is not None else None
    tenant_doc = tenant.document if tenant is not None else None
    entries = [
        *_stored_entries(install_doc, "install", "The install's stored policy"),
        *_stored_entries(tenant_doc, "tenant", f"The stored policy of tenant {tenant_id}"),
    ]
    return taxonomy_from_documents(install_doc, tenant_doc), entries


def _stored_entries(
    document: Mapping[str, Any] | None, scope: Scope, name: str
) -> list[ClassificationEntry]:
    """The entries of a stored document; a bad one names the document and the entry."""
    try:
        return entries_from_document(document, scope=scope)
    except ValueError as exc:
        raise InvalidStoredPolicyError(
            f"{name} is not valid, so MEHO cannot classify operations: {exc} "
            "Import a fixed policy file."
        ) from exc


async def _group_keys(
    session: AsyncSession,
    rows: Sequence[EndpointDescriptor],
    known: Mapping[UUID, str | None],
) -> dict[UUID, str | None]:
    """Return the group key of every row; one query for the ones *known* does not have."""
    keys: dict[UUID, str | None] = {}
    missing: set[UUID] = set()
    for row in rows:
        if row.id in known:
            keys[row.id] = known[row.id]
        elif row.group_id is None:
            keys[row.id] = None
        else:
            missing.add(row.group_id)
    if missing:
        result = await session.execute(
            select(OperationGroup.id, OperationGroup.group_key).where(
                OperationGroup.id.in_(missing)
            )
        )
        found = dict(result.tuples().all())
        for row in rows:
            if row.id not in keys:
                keys[row.id] = found.get(row.group_id) if row.group_id is not None else None
    return keys


async def classify_rows(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    rows: Sequence[EndpointDescriptor],
    overrides: Sequence[Sequence[OperationPolicyOverride]],
    group_keys: Mapping[UUID, str | None] | None = None,
) -> list[Classification]:
    """Return the classified values of each row in *rows*, in the same order.

    *overrides* holds the override rows of each row, in the same order.
    *group_keys* maps a row id to its group key when the caller already
    has it (a lookup that joined the group); the rest are loaded here.
    """
    taxonomy, entries = await load_classification(session, tenant_id)
    keys = await _group_keys(session, rows, group_keys or {})
    results: list[Classification] = []
    for row, row_overrides in zip(rows, overrides, strict=True):
        connector_id = connector_id_of(row)
        coords = OpCoordinates(connector_id, keys.get(row.id), row.op_id)
        all_entries = [*entries, *_override_entries(row_overrides, connector_id)]
        results.append(classify(coords, all_entries, taxonomy))
    return results


async def _stored_group_key(
    session: AsyncSession, tenant_id: UUID | None, step: EndpointDescriptor
) -> str | None:
    """The group key of *step*'s stored row, or ``None`` when it has none.

    The tenant's own row comes before a built-in one.
    """
    visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
    if tenant_id is not None:
        visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
    stmt = (
        select(OperationGroup.group_key)
        .join(EndpointDescriptor, EndpointDescriptor.group_id == OperationGroup.id)
        .where(
            visible,
            EndpointDescriptor.product == step.product,
            EndpointDescriptor.version == step.version,
            EndpointDescriptor.impl_id == step.impl_id,
            EndpointDescriptor.op_id == step.op_id,
        )
        .order_by(EndpointDescriptor.tenant_id.is_(None))
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def classify_step(
    session: AsyncSession, *, tenant_id: UUID | None, step: EndpointDescriptor
) -> Classification:
    """Return the classified values of one step of a multi-step operation.

    *step* is the in-memory row the step gate builds; it is never stored.
    Entries for the connector and the operation match it. The group key
    comes from the step's stored row when one exists, so a group entry
    matches too; without a stored row the step still runs, and only the
    group entries do not match.
    """
    taxonomy, entries = await load_classification(session, tenant_id)
    group_key = await _stored_group_key(session, tenant_id, step)
    coords = OpCoordinates(connector_id_of(step), group_key, step.op_id)
    return classify(coords, entries, taxonomy)


# ---------------------------------------------------------------------------
# One edit of a tenant's classification
# ---------------------------------------------------------------------------


class ClassificationEdit:
    """One requested change: what it matches and the values it sets."""

    def __init__(
        self,
        *,
        match: Mapping[str, Any],
        category: str | None,
        labels: Mapping[str, str] | None,
        always_wait: bool | None,
    ) -> None:
        self.match = {k: match[k] for k in ("connector", "group", "op") if match.get(k)}
        self.category = category
        self.labels = dict(labels) if labels is not None else None
        self.always_wait = always_wait

    def check(self, taxonomy: Taxonomy) -> None:
        """Refuse an edit with no pattern, no value, an unknown category or bad labels."""
        if not self.match:
            raise ValueError("`match` needs at least one of connector, group or op.")
        if self.category is None and self.labels is None and self.always_wait is None:
            raise ValueError("Set at least one of category, labels or always_wait.")
        if self.category is not None and self.category not in taxonomy.categories:
            known = ", ".join(taxonomy.categories)
            raise ValueError(f"Category {self.category!r} is not in the taxonomy ({known}).")
        if self.labels is not None:
            _check_labels(self.labels)


def _check_labels(labels: Mapping[str, str]) -> None:
    if len(labels) > _MAX_LABELS:
        raise ValueError(f"At most {_MAX_LABELS} labels are allowed.")
    for key, value in labels.items():
        if not key or len(key) > _MAX_LABEL_KEY:
            raise ValueError(f"Label key {key!r} must have 1 to {_MAX_LABEL_KEY} characters.")
        if not value or len(value) > _MAX_LABEL_VALUE:
            raise ValueError(
                f"The value of label {key!r} must have 1 to {_MAX_LABEL_VALUE} characters."
            )


def _merged_document(
    document: Mapping[str, Any] | None, edit: ClassificationEdit
) -> tuple[dict[str, Any], int]:
    """Return a copy of *document* with *edit* applied, and the index of the changed entry.

    An entry with the same ``match`` is updated: the given fields replace
    its values, the others stay. Otherwise a new entry is added at the end.
    """
    new_doc: dict[str, Any] = dict(document or {})
    classifications = [dict(e) for e in new_doc.get("classifications") or []]
    index = next(
        (i for i, e in enumerate(classifications) if e.get("match") == edit.match),
        len(classifications),
    )
    entry: dict[str, Any] = (
        classifications[index] if index < len(classifications) else {"match": edit.match}
    )
    if edit.category is not None:
        entry["category"] = edit.category
    if edit.labels is not None:
        entry["labels"] = edit.labels
    if edit.always_wait is not None:
        entry["always_wait"] = edit.always_wait
    if index == len(classifications):
        classifications.append(entry)
    new_doc["classifications"] = classifications
    return new_doc, index


async def _lock_tenant_policy(session: AsyncSession, tenant_id: UUID) -> None:
    """Make edits of one tenant's policy run one at a time (PostgreSQL only)."""
    conn = await session.connection()
    if conn.dialect.name != "postgresql":
        return
    label = f"meho:tenant-policy\0{tenant_id}"
    digest = hashlib.blake2b(label.encode(), digest_size=8).digest()
    key = int.from_bytes(digest, "big") & 0x7FFF_FFFF_FFFF_FFFF
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})


async def _visible_coords(session: AsyncSession, tenant_id: UUID) -> list[OpCoordinates]:
    """Every operation the tenant can see, as coordinates for matching."""
    group = aliased(OperationGroup)
    stmt = (
        select(
            EndpointDescriptor.impl_id,
            EndpointDescriptor.version,
            group.group_key,
            EndpointDescriptor.op_id,
        )
        .outerjoin(group, EndpointDescriptor.group_id == group.id)
        .where(
            or_(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.tenant_id == tenant_id,
            )
        )
    )
    return [
        OpCoordinates(f"{impl_id}-{version}", group_key, op_id)
        for impl_id, version, group_key, op_id in (await session.execute(stmt)).tuples()
    ]


async def _override_entries_for(
    session: AsyncSession, tenant_id: UUID
) -> list[ClassificationEntry]:
    """The category and labels overrides the tenant sees, as exact operation entries."""
    stmt = select(OperationPolicyOverride).where(
        or_(
            OperationPolicyOverride.tenant_id.is_(None),
            OperationPolicyOverride.tenant_id == tenant_id,
        ),
        or_(
            OperationPolicyOverride.category.is_not(None),
            OperationPolicyOverride.labels.is_not(None),
        ),
    )
    entries: list[ClassificationEntry] = []
    for override in (await session.execute(stmt)).scalars():
        entries.extend(_override_entries([override], f"{override.impl_id}-{override.version}"))
    return entries


@dataclass(frozen=True)
class EditPlan:
    """What one edit would do, worked out without writing anything.

    ``before`` and ``after`` hold every entry that applies to the tenant
    without and with the edit; ``matched`` the operations the changed
    entry matches, sorted and without repeats.
    """

    taxonomy: Taxonomy
    document: dict[str, Any]
    index: int
    before: list[ClassificationEntry]
    after: list[ClassificationEntry]
    matched: list[OpCoordinates]
    tenant_row: TenantPolicy | None

    @property
    def rule_ref(self) -> str:
        """Where the changed entry is in the tenant's document."""
        return f"tenant:classifications[{self.index}]"

    @property
    def entry(self) -> dict[str, Any]:
        """The changed entry as it would be stored."""
        entry: dict[str, Any] = self.document["classifications"][self.index]
        return entry


async def plan_edit(
    session: AsyncSession, *, tenant_id: UUID, edit: ClassificationEdit
) -> EditPlan:
    """Work out what *edit* changes for *tenant_id*; write nothing.

    Refuses (``ValueError``) an edit that is not valid. A conflict is not
    checked here: :func:`set_classification` refuses it, and a dry run
    shows it.
    """
    install, tenant = await _documents(session, tenant_id)
    install_doc = install.document if install is not None else None
    tenant_doc = tenant.document if tenant is not None else None
    taxonomy = taxonomy_from_documents(install_doc, tenant_doc)
    edit.check(taxonomy)
    new_doc, index = _merged_document(tenant_doc, edit)
    install_entries = entries_from_document(install_doc, scope="install")
    overrides = await _override_entries_for(session, tenant_id)
    new_tenant_entries = entries_from_document(new_doc, scope="tenant")
    changed = new_tenant_entries[index]
    matched = sorted(
        {c for c in await _visible_coords(session, tenant_id) if changed.matches(c)},
        key=lambda c: (c.connector_id, c.group_key or "", c.op_id),
    )
    return EditPlan(
        taxonomy=taxonomy,
        document=new_doc,
        index=index,
        before=[
            *install_entries,
            *entries_from_document(tenant_doc, scope="tenant"),
            *overrides,
        ],
        after=[*install_entries, *new_tenant_entries, *overrides],
        matched=matched,
        tenant_row=tenant,
    )


async def set_classification(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    set_by_sub: str,
    edit: ClassificationEdit,
) -> dict[str, Any]:
    """Store *edit* in the tenant's policy; return what changed.

    Refuses (``ValueError``, nothing written) an edit that is not valid,
    or that would give an operation two different categories or label
    values at the same level
    (:class:`~meho_backplane.policy.classification.ClassificationConflictError`).
    The caller owns the transaction.
    """
    await _lock_tenant_policy(session, tenant_id)
    plan = await plan_edit(session, tenant_id=tenant_id, edit=edit)
    check_no_conflict(plan.matched, plan.after, plan.taxonomy)
    tenant = plan.tenant_row
    if tenant is None:
        session.add(
            TenantPolicy(
                tenant_id=tenant_id,
                document=plan.document,
                set_by_sub=set_by_sub,
                set_at=datetime.now(UTC),
            )
        )
    else:
        tenant.document = plan.document
        tenant.set_by_sub = set_by_sub
        tenant.set_at = datetime.now(UTC)
    await session.flush()
    return {
        "rule_ref": plan.rule_ref,
        "entry": plan.entry,
        "matched_operations": len(plan.matched),
    }
