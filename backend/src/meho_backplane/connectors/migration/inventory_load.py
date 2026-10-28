# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Load inventories from stored results or from the call itself, and merge them.

An inventory read returns one :class:`~.contracts.InventoryEnvelope`. When
it is large, or when the read asks for it with the ``result_handle`` hint,
MEHO stores it whole as a stored result (a *document record* of the result
handle store) and the caller gets a handle id. The assessment operations
take those handle ids, and :func:`load_inventories` reads them back. They
also take inventories given inline in the call (#5143); those are checked
with the same rules as a stored one.

Rules:

* The store key carries the caller's MEHO tenant id, and the store checks
  the caller, so a handle of another tenant or another person is a miss.
* A miss (unknown, expired, another caller) fails with one clear message:
  a stored result lasts one hour by default, so the fix is to run the
  inventory read again.
* ``schema_version`` is checked before the rest, so an inventory of a
  newer shape is refused by name and not read wrongly. This applies to
  stored and inline inventories alike.
* Stored inventories come first, in handle order, then inline ones, in the
  given order. Merging keeps that order. A record whose ``source_id`` was seen
  before, or a server whose BIOS UUID was seen before, is kept in
  ``records`` but listed in ``duplicates``. A repeated record's BIOS UUID
  still counts, so a later record of the same machine from another
  source is found even when the first record had no BIOS UUID. The
  caller decides what a duplicate means for its output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from pydantic import ValidationError

from meho_backplane.connectors.migration.contracts import (
    INVENTORY_SCHEMA_VERSION,
    InventoryEnvelope,
    InventoryRecord,
    ServerRecord,
)
from meho_backplane.connectors.result_handle_store import (
    ResultHandleStore,
    ResultRecordKindError,
    get_result_handle_store,
)

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "MAX_INVENTORY_HANDLES",
    "Duplicate",
    "InventoryLoadError",
    "LoadedInventory",
    "load_inventories",
    "merge_inventories",
]

#: Most handles one call may merge.
MAX_INVENTORY_HANDLES: Final[int] = 20

_MAX_ERRORS_SHOWN: Final[int] = 3


class InventoryLoadError(ValueError):
    """A handle could not be read as an inventory. The message says how to fix it."""


@dataclass(frozen=True)
class Duplicate:
    """A record that names something already seen in an earlier record."""

    source_id: str
    same_as: str
    match: Literal["source_id", "bios_uuid"]


@dataclass(frozen=True)
class LoadedInventory:
    """All inventories, in merge order, and their merged records.

    ``handle_ids`` has one entry per inventory in ``envelopes``: the handle
    id of a stored inventory, or ``None`` for an inline one.
    """

    handle_ids: list[str | None]
    envelopes: list[InventoryEnvelope]
    records: list[InventoryRecord] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)

    def sources(self) -> list[dict[str, Any]]:
        """Where each inventory came from, for the ``sources`` of a report.

        ``origin`` is ``"stored"`` (with its ``handle_id``) or ``"inline"``
        (no handle id), so a report shows which data MEHO stored itself.
        """
        sources: list[dict[str, Any]] = []
        for handle_id, envelope in zip(self.handle_ids, self.envelopes, strict=True):
            entry: dict[str, Any] = (
                {"origin": "inline"}
                if handle_id is None
                else {"origin": "stored", "handle_id": handle_id}
            )
            entry["connector_id"] = envelope.source.connector_id
            entry["target"] = envelope.source.target
            entry["collected_at"] = envelope.source.collected_at.isoformat()
            sources.append(entry)
        return sources


def _not_found(handle_id: UUID) -> InventoryLoadError:
    return InventoryLoadError(
        f"inventory handle {handle_id} was not found or has expired. A stored result "
        "lasts one hour by default; run the inventory read again and use the new handle."
    )


def _parse_handle_id(raw: str) -> UUID:
    try:
        return UUID(str(raw))
    except ValueError:
        raise InventoryLoadError(f"{raw!r} is not a handle id (a UUID)") from None


def _validation_summary(exc: ValidationError) -> str:
    # include_input=False: the values come from source systems, so they stay
    # out of the message.
    errors = exc.errors(include_input=False, include_url=False)
    shown = "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
        for error in errors[:_MAX_ERRORS_SHOWN]
    )
    more = len(errors) - _MAX_ERRORS_SHOWN
    return shown + (f"; and {more} more" if more > 0 else "")


def _decode_envelope(
    store: ResultHandleStore, raw: bytes, *, operator_sub: str, handle_id: UUID
) -> InventoryEnvelope:
    """Decode and check one stored record (sync, runs in a worker thread)."""
    try:
        stored = store.decode_document(raw, operator_sub=operator_sub, handle_id=handle_id)
    except ResultRecordKindError:
        raise InventoryLoadError(
            f"handle {handle_id} holds rows, not one inventory. Pass the handle that "
            "an inventory read returned."
        ) from None
    if stored is None:
        raise _not_found(handle_id)
    return _check_inventory(stored.document, f"handle {handle_id}")


def _check_inventory(document: Any, label: str) -> InventoryEnvelope:
    """Check one inventory, stored or inline: schema version first, then the shape.

    *label* names it in the message (``handle <id>`` or ``inline inventory <n>``).
    Sync; runs in a worker thread.
    """
    if not isinstance(document, dict):
        raise InventoryLoadError(f"{label} does not hold an inventory")
    version = document.get("schema_version")
    if version != INVENTORY_SCHEMA_VERSION:
        raise InventoryLoadError(
            f"{label} holds inventory schema version {version!r}; "
            f"this MEHO reads version {INVENTORY_SCHEMA_VERSION!r}"
        )
    try:
        return InventoryEnvelope.model_validate(document)
    except ValidationError as exc:
        raise InventoryLoadError(
            f"{label} does not hold a valid inventory: {_validation_summary(exc)}"
        ) from None


def merge_inventories(
    envelopes: list[InventoryEnvelope],
) -> tuple[list[InventoryRecord], list[Duplicate]]:
    records: list[InventoryRecord] = []
    duplicates: list[Duplicate] = []
    by_source_id: set[str] = set()
    by_bios_uuid: dict[str, str] = {}
    for envelope in envelopes:
        for record in envelope.records:
            records.append(record)
            if record.source_id in by_source_id:
                duplicates.append(Duplicate(record.source_id, record.source_id, "source_id"))
            else:
                by_source_id.add(record.source_id)
            # A repeated record still registers its BIOS UUID: the first record
            # may lack it, and a later source may report the same machine.
            if not isinstance(record, ServerRecord) or not record.identity_hints.bios_uuid:
                continue
            first = by_bios_uuid.setdefault(record.identity_hints.bios_uuid, record.source_id)
            if first != record.source_id:
                duplicates.append(Duplicate(record.source_id, first, "bios_uuid"))
    return records, duplicates


async def load_inventories(
    operator: Operator,
    handle_ids: list[str] | None = None,
    *,
    inventories: list[Any] | None = None,
    store: ResultHandleStore | None = None,
) -> LoadedInventory:
    """Read the stored and the inline inventories and merge them.

    Each handle is read from *operator*'s tenant; a handle given twice is
    read once. Each of *inventories* is an inventory given inline in the
    call. At least one handle or inline inventory is needed, and at most
    :data:`MAX_INVENTORY_HANDLES` of both together.

    Raises :class:`InventoryLoadError` for the first handle that is not a
    UUID, is missing or expired, or for the first inventory (stored or
    inline) that is not an inventory or has another schema version.
    """
    handle_ids = handle_ids or []
    inventories = inventories or []
    if not handle_ids and not inventories:
        raise InventoryLoadError("give at least one inventory handle or one inline inventory")
    if len(handle_ids) + len(inventories) > MAX_INVENTORY_HANDLES:
        raise InventoryLoadError(
            f"give at most {MAX_INVENTORY_HANDLES} inventories, handles and inline ones together"
        )
    resolved = store if store is not None else get_result_handle_store()
    ordered = list(dict.fromkeys(_parse_handle_id(raw) for raw in handle_ids))
    envelopes: list[InventoryEnvelope] = []
    for handle_id in ordered:
        raw = await resolved.fetch_record(tenant_id=operator.tenant_id, handle_id=handle_id)
        if raw is None:
            raise _not_found(handle_id)
        envelopes.append(
            await asyncio.to_thread(
                _decode_envelope, resolved, raw, operator_sub=operator.sub, handle_id=handle_id
            )
        )
    for number, document in enumerate(inventories, start=1):
        envelopes.append(
            await asyncio.to_thread(_check_inventory, document, f"inline inventory {number}")
        )
    records, duplicates = merge_inventories(envelopes)
    return LoadedInventory(
        handle_ids=[str(handle_id) for handle_id in ordered] + [None] * len(inventories),
        envelopes=envelopes,
        records=records,
        duplicates=duplicates,
    )
