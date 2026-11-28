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
runs after an admin approved the change, or at once for an admin in the
web console (the
``policy.classify`` operation, :mod:`meho_backplane.connectors.policy`).

Nothing here runs while the ``POLICY_CLASSIFICATION`` switch is off
(:func:`classification_enabled`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol
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
    LABEL_KEY_MAX,
    LABEL_VALUE_MAX,
    Classification,
    ClassificationEntry,
    OpCoordinates,
    Scope,
    Taxonomy,
    check_no_conflict,
    classify,
    entries_from_document,
    install_wait_text,
    install_wait_undone,
    label_text_ok,
    taxonomy_from_documents,
)
from meho_backplane.policy.switches import classification_on, custom_policy_on
from meho_backplane.settings import get_settings

__all__ = [
    "SWITCH_OFF_REFUSAL",
    "ClassificationEdit",
    "EditPlan",
    "InvalidStoredPolicyError",
    "StepGroups",
    "classification_enabled",
    "classify_rows",
    "classify_step",
    "connector_id_of",
    "coordinates_of",
    "load_classification",
    "load_step_groups",
    "plan_edit",
    "product_form_on",
    "set_classification",
    "step_group_key",
]

_MAX_LABELS = 32


class InvalidStoredPolicyError(ValueError):
    """A stored policy document is not valid, so no operation can be classified."""


#: What every surface says while the switch is off.
SWITCH_OFF_REFUSAL: Final[str] = (
    "Classification is off on this MEHO. An admin can turn it on in one of two ways. "
    "The POLICY_CLASSIFICATION setting turns on classification only. The CUSTOM_POLICY "
    "setting turns on all three policy parts: classification, the policy evaluator and "
    "approver routing."
)


def classification_enabled() -> bool:
    """True when ``POLICY_CLASSIFICATION`` or ``CUSTOM_POLICY`` is on."""
    return classification_on(get_settings())


def product_form_on() -> bool:
    """True when a connector with no version is named by its product (``CUSTOM_POLICY`` on)."""
    return custom_policy_on(get_settings())


class ConnectorRow(Protocol):
    """A row that names a connector: an operation row or an override row."""

    @property
    def product(self) -> str:
        """The product, for example ``vault``."""

    @property
    def version(self) -> str:
        """The version; empty for a connector with no version."""

    @property
    def impl_id(self) -> str:
        """The implementation id; empty for a connector with no version."""


class OperationRow(ConnectorRow, Protocol):
    """A row that names one operation of a connector."""

    @property
    def op_id(self) -> str:
        """The operation id."""


def _dashed_id(row: ConnectorRow) -> str:
    return f"{row.impl_id}-{row.version}"


def connector_id_of(row: ConnectorRow, *, product_form: bool | None = None) -> str:
    """The connector id of *row* in the policy.

    ``<impl_id>-<version>``. With ``CUSTOM_POLICY`` on
    (:func:`product_form_on`), a connector with no implementation id or no
    version is named by its ``<product>`` instead, the id a call uses
    (:func:`~meho_backplane.operations._lookup.lookup_descriptor`). Every
    reader of the policy names a connector with this function.
    *product_form* ``True`` names such a connector by its product whatever
    the switch says; ``None`` reads the switch.
    """
    if product_form is None:
        product_form = product_form_on()
    if (row.impl_id and row.version) or not product_form:
        return _dashed_id(row)
    return row.product


def coordinates_of(
    row: OperationRow, group_key: str | None, *, product_form: bool | None = None
) -> OpCoordinates:
    """What a policy entry matches for the operation of *row*, in group *group_key*.

    When :func:`connector_id_of` names the connector by its product, the
    coordinates also hold the form ``<impl_id>-<version>``. The operation is
    then classified once per form, and the stricter result counts
    (:func:`~meho_backplane.policy.classification.classify`).
    *product_form* is passed on to :func:`connector_id_of`.
    """
    connector_id = connector_id_of(row, product_form=product_form)
    dashed = _dashed_id(row)
    return OpCoordinates(
        connector_id, group_key, row.op_id, dashed if dashed != connector_id else None
    )


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
    overrides: Sequence[OperationPolicyOverride],
    connector_id: str,
    *,
    for_connector: str | None = None,
) -> list[ClassificationEntry]:
    """The ``category`` and ``labels`` of override rows, as exact operation entries.

    *for_connector* ties each entry to the operations of that connector id
    (:attr:`ClassificationEntry.for_connector`).
    """
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
                for_connector=for_connector,
            )
        )
    return entries


def _override_entries_at(
    overrides: Sequence[OperationPolicyOverride], coords: OpCoordinates
) -> list[ClassificationEntry]:
    """:func:`_override_entries` once per form of *coords*, so each form matches its own.

    For an operation with two forms, each entry is tied to the connector
    id of *coords*: another connector with the same form with the dash
    does not match it.
    """
    forms = coords.forms()
    tie = coords.connector_id if len(forms) > 1 else None
    return [
        entry
        for form in forms
        for entry in _override_entries(overrides, form.connector_id, for_connector=tie)
    ]


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
        coords = coordinates_of(row, keys.get(row.id))
        all_entries = [*entries, *_override_entries_at(row_overrides, coords)]
        results.append(classify(coords, all_entries, taxonomy))
    return results


_StepKey = tuple[str, str, str, str]


def _step_key(step: OperationRow) -> _StepKey:
    return (step.product, step.version, step.impl_id, step.op_id)


@dataclass(frozen=True)
class StepGroups:
    """The stored groups of steps of multi-step operations, read in one query.

    ``rows`` maps a step (product, version, implementation id, op id) to
    the group key of each stored row of it, by tenant (``None``: the
    built-in row). A row in no group has ``None``. Read it with
    :func:`load_step_groups`.
    """

    rows: Mapping[_StepKey, Mapping[UUID | None, str | None]]

    def group_of(self, step: OperationRow, tenant_id: UUID | None) -> str | None:
        """The group of *step* for a run of *tenant_id*.

        The group of the tenant's own row of the step first, then the
        group of the built-in row, and no group without a row. The step
        gate, the step preview and the carry-over dry run all use this.
        """
        by_tenant = self.rows.get(_step_key(step), {})
        own = by_tenant.get(tenant_id) if tenant_id is not None else None
        return own if own is not None else by_tenant.get(None)

    def tenants_with_own_row(self, step: OperationRow) -> list[UUID]:
        """Every tenant with its own stored row of *step*, sorted."""
        by_tenant = self.rows.get(_step_key(step), {})
        return sorted(t for t in by_tenant if t is not None)


async def load_step_groups(
    session: AsyncSession,
    steps: Iterable[OperationRow],
    *,
    tenant_id: UUID | None = None,
    every_tenant: bool = False,
) -> StepGroups:
    """Read the stored groups of *steps* in one query.

    It reads the built-in rows and the rows of *tenant_id*, or the rows of
    every tenant when *every_tenant* is true.
    """
    wanted = {_step_key(step) for step in steps}
    if not wanted:
        return StepGroups({})
    stmt = (
        select(
            EndpointDescriptor.tenant_id,
            EndpointDescriptor.product,
            EndpointDescriptor.version,
            EndpointDescriptor.impl_id,
            EndpointDescriptor.op_id,
            OperationGroup.group_key,
        )
        .outerjoin(OperationGroup, EndpointDescriptor.group_id == OperationGroup.id)
        .where(EndpointDescriptor.op_id.in_({key[3] for key in wanted}))
    )
    if not every_tenant:
        visible: ColumnElement[bool] = EndpointDescriptor.tenant_id.is_(None)
        if tenant_id is not None:
            visible = or_(visible, EndpointDescriptor.tenant_id == tenant_id)
        stmt = stmt.where(visible)
    rows: dict[_StepKey, dict[UUID | None, str | None]] = {}
    for row_tenant, product, version, impl_id, op_id, group_key in (
        await session.execute(stmt)
    ).tuples():
        key = (product, version, impl_id, op_id)
        if key in wanted:
            rows.setdefault(key, {})[row_tenant] = group_key
    return StepGroups(rows)


async def step_group_key(
    session: AsyncSession, tenant_id: UUID | None, step: OperationRow
) -> str | None:
    """The group of *step* for a run of *tenant_id* (:meth:`StepGroups.group_of`)."""
    groups = await load_step_groups(session, [step], tenant_id=tenant_id)
    return groups.group_of(step, tenant_id)


async def classify_step(
    session: AsyncSession,
    *,
    tenant_id: UUID | None,
    step: EndpointDescriptor,
    overrides: Sequence[OperationPolicyOverride] = (),
) -> Classification:
    """Return the classified values of one step of a multi-step operation.

    *step* is the in-memory row the step gate builds; it is never stored.
    Entries for the connector and the operation match it. The group key
    comes from the step's stored row when one exists, so a group entry
    matches too; without a stored row the step still runs, and only the
    group entries do not match. *overrides* are the override rows of the
    step's operation: their ``category`` and ``labels`` count as for a
    direct call (:func:`classify_rows`).
    """
    taxonomy, entries = await load_classification(session, tenant_id)
    coords = coordinates_of(step, await step_group_key(session, tenant_id, step))
    return classify(coords, [*entries, *_override_entries_at(overrides, coords)], taxonomy)


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
        if not key or len(key) > LABEL_KEY_MAX:
            raise ValueError(f"Label key {key!r} must have 1 to {LABEL_KEY_MAX} characters.")
        if not value or len(value) > LABEL_VALUE_MAX:
            raise ValueError(
                f"The value of label {key!r} must have 1 to {LABEL_VALUE_MAX} characters."
            )
        if not label_text_ok(key, value):
            raise ValueError(
                f"Label {key!r}: a key may use only letters, digits and . _ / -, "
                "and a value only printable characters with no line breaks."
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
            EndpointDescriptor.product,
            EndpointDescriptor.version,
            EndpointDescriptor.impl_id,
            EndpointDescriptor.op_id,
            group.group_key,
        )
        .outerjoin(group, EndpointDescriptor.group_id == group.id)
        .where(
            or_(
                EndpointDescriptor.tenant_id.is_(None),
                EndpointDescriptor.tenant_id == tenant_id,
            )
        )
    )
    return [coordinates_of(row, row.group_key) for row in (await session.execute(stmt)).all()]


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
        entries.extend(_override_entries_at([override], coordinates_of(override, None)))
    return entries


@dataclass(frozen=True)
class EditPlan:
    """What one edit would do, worked out without writing anything.

    ``before`` and ``after`` hold every entry that applies to the tenant
    without and with the edit; ``matched`` the operations the changed
    entry matches, sorted and without repeats. ``install_wait_refusal``
    is the reason the edit is refused when it sets ``always_wait: false``
    on an operation that the install's policy makes always wait.
    """

    taxonomy: Taxonomy
    document: dict[str, Any]
    index: int
    before: list[ClassificationEntry]
    after: list[ClassificationEntry]
    matched: list[OpCoordinates]
    tenant_row: TenantPolicy | None
    install_wait_refusal: str | None = None

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

    Refuses (``ValueError``) an edit that is not valid. A conflict and an
    edit that sets back an install-wide "always wait" are not refused
    here: :func:`set_classification` refuses them, and a dry run shows
    them.
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
    undone = (
        install_wait_undone(changed, matched, install_entries)
        if edit.always_wait is False
        else None
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
        install_wait_refusal=(
            install_wait_text("This edit", *undone, single_edit=True) if undone else None
        ),
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
    (:class:`~meho_backplane.policy.classification.ClassificationConflictError`),
    or that sets back an "always wait" of the install's policy.
    The caller owns the transaction.
    """
    await _lock_tenant_policy(session, tenant_id)
    plan = await plan_edit(session, tenant_id=tenant_id, edit=edit)
    if plan.install_wait_refusal is not None:
        raise ValueError(plan.install_wait_refusal)
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
