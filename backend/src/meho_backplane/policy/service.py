# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Plan, apply, export and history of the policy file (``policy: v1``).

* :func:`build_plan` checks a file and works out what it would change. It
  reads the database and writes nothing. The preview, the validation and
  the import request are this plan.
* :func:`apply_policy` stores the file. It runs only after a second admin
  approved the import (the ``policy.apply`` operation,
  :mod:`meho_backplane.connectors.policy.apply`). It plans again first:
  when the stored policy changed after the plan, nothing is written.
* :func:`export_policy` writes the stored policy as a file. A tenant
  entry that tries to set back an install-wide "always wait" gets a
  comment that says it has no effect.
* :func:`list_history` returns the imported files, newest first.

The import replaces the whole policy of its scope (a tenant, or the
install): an entry that is not in the file is removed. The same file twice,
or a file with the same meaning, is "No change.": nothing waits and no
revision is written.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.db.models import PolicyRevision, TenantPolicy
from meho_backplane.policy.bindings import routing_enabled, shipped_binding_documents
from meho_backplane.policy.classification import (
    Classification,
    ClassificationEntry,
    Scope,
    Taxonomy,
    classify,
    entries_from_document,
    install_wait_text,
    install_wait_undone,
    taxonomy_from_documents,
)
from meho_backplane.policy.document import (
    POLICY_VERSION,
    canonical_sha256,
    document_of,
    file_sha256,
    normalise_document,
    parse_policy_file,
)
from meho_backplane.policy.store import _lock_tenant_policy, _override_entries_for
from meho_backplane.policy.validate import KnownOp, check_document, load_known_ops
from meho_backplane.settings import get_settings

__all__ = [
    "KEEP_REVISIONS",
    "NO_CHANGE",
    "POLICY_CHANGED",
    "PolicyApplyError",
    "PolicyPlan",
    "apply_policy",
    "build_plan",
    "export_policy",
    "list_history",
]

#: The answer to a file that changes nothing.
NO_CHANGE: Final[str] = "No change."

#: Why an approved import did not run: the stored policy moved after the plan.
POLICY_CHANGED: Final[str] = (
    "The policy changed after this import was planned. Run the preview again."
)

#: How many imported files MEHO keeps per tenant (and for the install).
KEEP_REVISIONS: Final[int] = 20

#: The lock key of the install's policy (a tenant id is never all zeros).
_INSTALL_LOCK_ID: Final[uuid.UUID] = uuid.UUID(int=0)


class PolicyApplyError(ValueError):
    """An approved import did not run; the message says why in one sentence."""


@dataclass
class PolicyPlan:
    """What a file would change, or why it cannot be imported."""

    scope: Scope
    tenant_id: uuid.UUID | None
    file_sha256: str
    #: The SHA-256 of the stored policy's meaning when the plan was made.
    based_on: str
    mistakes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    diff: dict[str, Any] | None = None
    document: dict[str, Any] | None = None
    no_change: bool = False


async def _stored_row(
    session: AsyncSession, scope: Scope, tenant_id: uuid.UUID | None
) -> TenantPolicy | None:
    column = TenantPolicy.tenant_id
    where = column.is_(None) if scope == "install" else column == tenant_id
    return (await session.execute(select(TenantPolicy).where(where))).scalar_one_or_none()


def _stored_name(scope: Scope, tenant_id: uuid.UUID | None) -> str:
    return (
        "the install's stored policy"
        if scope == "install"
        else (f"the stored policy of tenant {tenant_id}")
    )


def _entries_or_note(
    document: dict[str, Any] | None, scope: Scope, name: str, notes: list[str]
) -> list[ClassificationEntry]:
    """The entries of a stored *document*; a bad one adds a note and counts as empty."""
    try:
        return entries_from_document(document, scope=scope)
    except ValueError as exc:
        notes.append(f"{name[:1].upper()}{name[1:]} is not valid: {exc} The import replaces it.")
        return []


@dataclass(frozen=True)
class _Side:
    """The entries and the taxonomy on one side of the diff."""

    entries: list[ClassificationEntry]
    taxonomy: Taxonomy


def _values(result: Classification) -> tuple[Any, ...]:
    return (
        result.category,
        dict(result.labels),
        result.risk,
        result.always_wait,
        result.conflict,
    )


def _match_key(entry: Any) -> str:
    """The key of *entry* in the diff: two different matches never share one.

    Each part is quoted (JSON), so no pattern text can look like another
    part. A stored entry that is not a map, or whose ``match`` is not a
    map, keys on its whole text: the import shows it as removed.
    """
    match = entry.get("match") if isinstance(entry, dict) else None
    if not isinstance(match, dict):
        return json.dumps(["not-valid", entry], sort_keys=True, default=str)
    return json.dumps([match.get(k) or None for k in ("connector", "group", "op")])


def _raw_entries(document: dict[str, Any]) -> list[Any]:
    """The ``classifications`` of *document*; a value that is not a list is no entry."""
    raw = document.get("classifications")
    return list(raw) if isinstance(raw, list) else []


def _counts(raw: Any, scope: Scope, ops: Sequence[KnownOp], changed: set[int]) -> dict[str, Any]:
    """How many operations *raw* matches, and how many of those change.

    Both are ``None`` for a stored entry that is not valid (it matched nothing).
    """
    try:
        probe = entries_from_document({"classifications": [raw]}, scope=scope)[0]
    except (ValueError, IndexError):
        return {"matches": None, "changes": None}
    hits = [i for i, op in enumerate(ops) if probe.matches(op.coords)]
    return {"matches": len(hits), "changes": sum(1 for i in hits if i in changed)}


def _classification_diff(
    before: list[Any],
    after: list[Any],
    *,
    scope: Scope,
    ops: Sequence[KnownOp],
    changed: set[int],
) -> dict[str, list[dict[str, Any]]]:
    old = {_match_key(e): e for e in before}
    new = {_match_key(e): e for e in after}
    out: dict[str, list[dict[str, Any]]] = {"added": [], "changed": [], "removed": []}
    for key, entry in new.items():
        if key not in old:
            out["added"].append({"entry": entry, **_counts(entry, scope, ops, changed)})
        elif normalise_document(old[key]) != normalise_document(entry):
            out["changed"].append(
                {"before": old[key], "after": entry, **_counts(entry, scope, ops, changed)}
            )
    for key, entry in old.items():
        if key not in new:
            out["removed"].append({"entry": entry, **_counts(entry, scope, ops, changed)})
    return out


def _taxonomy_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old_tax, new_tax = before.get("taxonomy") or {}, after.get("taxonomy") or {}
    old_cats, new_cats = old_tax.get("categories") or [], new_tax.get("categories") or []
    changed = []
    if old_tax.get("default") != new_tax.get("default"):
        changed.append(
            {"key": "default", "before": old_tax.get("default"), "after": new_tax.get("default")}
        )
    return {
        "added": [c for c in new_cats if c not in old_cats],
        "changed": changed,
        "removed": [c for c in old_cats if c not in new_cats],
    }


def _binding_key(binding: dict[str, Any]) -> str:
    key = [binding.get("principal") or "person", binding.get("match") or {}]
    return json.dumps(key, sort_keys=True)


def _bindings_diff(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> dict[str, list[dict[str, Any]]]:
    """Added, changed and removed bindings, by login kind and ``match``."""
    old = {_binding_key(b): b for b in before}
    new = {_binding_key(b): b for b in after}
    return {
        "added": [{"entry": b} for k, b in new.items() if k not in old],
        "changed": [
            {"before": old[k], "after": b}
            for k, b in new.items()
            if k in old and normalise_document(old[k]) != normalise_document(b)
        ],
        "removed": [{"entry": b} for k, b in old.items() if k not in new],
    }


def _description_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    old, new = before.get("description"), after.get("description")
    if old == new:
        return {"added": [], "changed": [], "removed": []}
    if old is None:
        return {"added": [new], "changed": [], "removed": []}
    if new is None:
        return {"added": [], "changed": [], "removed": [old]}
    return {"added": [], "changed": [{"before": old, "after": new}], "removed": []}


def _sides(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    scope: Scope,
    install_document: dict[str, Any] | None,
    other_entries: Sequence[ClassificationEntry],
    notes: list[str],
    name: str,
) -> tuple[_Side, _Side]:
    old_entries = _entries_or_note(before, scope, name, notes)
    new_entries = entries_from_document(after, scope=scope)
    if scope == "install":
        return (
            _Side(old_entries, taxonomy_from_documents(before, None)),
            _Side(new_entries, taxonomy_from_documents(after, None)),
        )
    base = [
        *_entries_or_note(install_document, "install", "the install's stored policy", []),
        *other_entries,
    ]
    return (
        _Side([*base, *old_entries], taxonomy_from_documents(install_document, before)),
        _Side([*base, *new_entries], taxonomy_from_documents(install_document, after)),
    )


def _build_diff(
    before: dict[str, Any] | None,
    after: dict[str, Any],
    *,
    scope: Scope,
    ops: Sequence[KnownOp],
    old_side: _Side,
    new_side: _Side,
) -> dict[str, Any]:
    changed = {
        i
        for i, op in enumerate(ops)
        if _values(classify(op.coords, old_side.entries, old_side.taxonomy))
        != _values(classify(op.coords, new_side.entries, new_side.taxonomy))
    }
    old = normalise_document(before)
    return {
        "description": _description_diff(old, after),
        "taxonomy": _taxonomy_diff(old, after),
        "classifications": _classification_diff(
            _raw_entries(old),
            _raw_entries(after),
            scope=scope,
            ops=ops,
            changed=changed,
        ),
        "bindings": _bindings_diff(
            list(old.get("bindings") or []), list(after.get("bindings") or [])
        ),
        "operations_changed": len(changed),
    }


async def build_plan(
    session: AsyncSession,
    *,
    scope: Scope,
    tenant_id: uuid.UUID | None,
    content: bytes,
) -> PolicyPlan:
    """Check *content* for *scope* and work out what it would change. Writes nothing."""
    stored = await _stored_row(session, scope, tenant_id)
    before = stored.document if stored is not None else None
    plan = PolicyPlan(
        scope=scope,
        tenant_id=tenant_id,
        file_sha256=file_sha256(content),
        based_on=canonical_sha256(before),
    )
    parsed = parse_policy_file(content)
    if parsed.model is None:
        plan.mistakes = parsed.mistakes
        return plan
    document = document_of(parsed.model)
    install_document: dict[str, Any] | None = document
    other: list[ClassificationEntry] = []
    if scope == "tenant":
        install_row = await _stored_row(session, "install", None)
        install_document = install_row.document if install_row is not None else None
        if tenant_id is not None:
            other = await _override_entries_for(session, tenant_id)
    ops = await load_known_ops(session, scope=scope, tenant_id=tenant_id)
    try:
        plan.mistakes = check_document(
            document,
            scope=scope,
            install_document=install_document,
            ops=ops,
            other_entries=other,
        )
    except ValueError as exc:
        plan.mistakes = [f"The install's stored policy is not valid: {exc} Import it again first."]
    if plan.mistakes:
        return plan
    old_side, new_side = _sides(
        before,
        document,
        scope=scope,
        install_document=install_document,
        other_entries=other,
        notes=plan.warnings,
        name=_stored_name(scope, tenant_id),
    )
    plan.document = document
    plan.no_change = canonical_sha256(document) == plan.based_on
    plan.diff = _build_diff(
        before, document, scope=scope, ops=ops, old_side=old_side, new_side=new_side
    )
    return plan


def _summary(diff: dict[str, Any]) -> dict[str, int]:
    return {
        f"{section}_{kind}": len(diff[section][kind])
        for section in ("description", "taxonomy", "classifications", "bindings")
        for kind in ("added", "changed", "removed")
    }


async def _keep_last_revisions(session: AsyncSession, scope_tenant: uuid.UUID | None) -> None:
    column = PolicyRevision.tenant_id
    where = column.is_(None) if scope_tenant is None else column == scope_tenant
    old_ids = (
        (
            await session.execute(
                select(PolicyRevision.id)
                .where(where)
                .order_by(PolicyRevision.ts.desc(), PolicyRevision.id.desc())
                .offset(KEEP_REVISIONS)
            )
        )
        .scalars()
        .all()
    )
    if old_ids:
        await session.execute(delete(PolicyRevision).where(PolicyRevision.id.in_(old_ids)))


async def apply_policy(
    session: AsyncSession,
    *,
    scope: Scope,
    tenant_id: uuid.UUID | None,
    content: bytes,
    expected_sha256: str,
    based_on: str,
    imported_by: str,
    approved_by: str,
) -> dict[str, Any]:
    """Store the file after the approval; return what happened.

    Raises :class:`PolicyApplyError` (nothing written) when the file does
    not match its SHA-256, when the stored policy changed after the plan,
    or when the file has a mistake now. The caller owns the transaction.
    """
    if file_sha256(content) != expected_sha256:
        raise PolicyApplyError("The file does not match its SHA-256. Run the import again.")
    lock_id = tenant_id if scope == "tenant" and tenant_id is not None else _INSTALL_LOCK_ID
    await _lock_tenant_policy(session, lock_id)
    plan = await build_plan(session, scope=scope, tenant_id=tenant_id, content=content)
    if plan.based_on != based_on:
        raise PolicyApplyError(POLICY_CHANGED)
    if plan.mistakes or plan.document is None or plan.diff is None:
        raise PolicyApplyError(f"The file cannot be imported now: {plan.mistakes[0]}")
    if plan.no_change:
        return {"scope": scope, "applied": False, "message": NO_CHANGE, "revision_id": None}
    scope_tenant = tenant_id if scope == "tenant" else None
    now = datetime.now(UTC)
    stored = await _stored_row(session, scope, tenant_id)
    if stored is None:
        session.add(
            TenantPolicy(
                tenant_id=scope_tenant, document=plan.document, set_by_sub=imported_by, set_at=now
            )
        )
    else:
        stored.document = plan.document
        stored.set_by_sub = imported_by
        stored.set_at = now
    revision = PolicyRevision(
        id=uuid.uuid4(),
        tenant_id=scope_tenant,
        ts=now,
        imported_by_sub=imported_by,
        approved_by_sub=approved_by,
        file_sha256=plan.file_sha256,
        file_text=content.decode("utf-8"),
        document=plan.document,
        diff=plan.diff,
    )
    session.add(revision)
    await session.flush()
    await _keep_last_revisions(session, scope_tenant)
    return {
        "scope": scope,
        "applied": True,
        "message": "The policy file is imported.",
        "revision_id": str(revision.id),
        "summary": _summary(plan.diff),
    }


class _NoAliasDumper(yaml.SafeDumper):  # type: ignore[misc]  # yaml has no stubs here
    """Write each value out: the import refuses anchors and aliases."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def _dump(value: Any) -> str:
    text: str = yaml.dump(
        value, Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return text


async def _no_effect_notes(
    session: AsyncSession, tenant_id: uuid.UUID | None, entries: list[dict[str, Any]]
) -> dict[int, str]:
    """The tenant entries that try to set back an install-wide "always wait", by index."""
    install_row = await _stored_row(session, "install", None)
    install_entries = _entries_or_note(
        install_row.document if install_row is not None else None,
        "install",
        "the install's stored policy",
        [],
    )
    if not any(e.always_wait for e in install_entries):
        return {}
    ops = await load_known_ops(session, scope="tenant", tenant_id=tenant_id)
    notes: dict[int, str] = {}
    for index, raw in enumerate(entries):
        try:
            entry = entries_from_document({"classifications": [raw]}, scope="tenant")[0]
        except (ValueError, IndexError):
            continue
        found = install_wait_undone(entry, (op.coords for op in ops), install_entries)
        if found is not None:
            notes[index] = install_wait_text(f"`classifications[{index}]`", *found)
    return notes


async def export_policy(session: AsyncSession, *, scope: Scope, tenant_id: uuid.UUID | None) -> str:
    """The stored policy of *scope* as a ``policy: v1`` file.

    In a tenant's file, an entry that tries to set back an "always wait"
    of the install's policy gets a ``# Has no effect:`` comment above it.

    A tenant's file holds its stored bindings. When it has none and the
    ``POLICY_APPROVER_ROUTING`` switch is on, it holds the shipped bindings
    (:func:`~meho_backplane.policy.bindings.shipped_binding_documents`),
    the ones that are in effect.
    """
    stored = await _stored_row(session, scope, tenant_id)
    document = normalise_document(stored.document if stored is not None else None)
    head: dict[str, Any] = {"policy": POLICY_VERSION}
    for key in ("description", "taxonomy"):
        if key in document:
            head[key] = document[key]
    tail: dict[str, Any] = {}
    if document.get("bindings"):
        tail["bindings"] = list(document["bindings"])
    elif scope == "tenant" and routing_enabled():
        tail["bindings"] = shipped_binding_documents(get_settings())
    entries = list(document.get("classifications") or [])
    if not entries:
        return _dump({**head, "classifications": [], **tail})
    notes = await _no_effect_notes(session, tenant_id, entries) if scope == "tenant" else {}
    parts = [_dump(head), "classifications:\n"]
    for index, entry in enumerate(entries):
        if index in notes:
            parts.append(f"# Has no effect: {notes[index]}\n")
        parts.append(_dump([entry]))
    if tail:
        parts.append(_dump(tail))
    return "".join(parts)


async def list_history(
    session: AsyncSession, *, scope: Scope, tenant_id: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    """The imported files of *scope*, newest first."""
    column = PolicyRevision.tenant_id
    where = column.is_(None) if scope == "install" else column == tenant_id
    rows = (
        await session.execute(
            select(PolicyRevision)
            .where(where)
            .order_by(PolicyRevision.ts.desc(), PolicyRevision.id.desc())
            .limit(limit)
        )
    ).scalars()
    return [
        {
            "id": str(row.id),
            "ts": row.ts,
            "scope": scope,
            "imported_by_sub": row.imported_by_sub,
            "approved_by_sub": row.approved_by_sub,
            "file_sha256": row.file_sha256,
            "file": row.file_text,
            "diff": row.diff,
        }
        for row in rows
    ]
