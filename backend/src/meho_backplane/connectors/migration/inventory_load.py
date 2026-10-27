# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Load inventories from stored results and merge them.

An inventory read returns one :class:`~.contracts.InventoryEnvelope`. When
it is large, MEHO stores it whole as a stored result (a *document record*
of the result handle store) and the caller gets a handle id. The
assessment operations take those handle ids, and :func:`load_inventories`
reads them back.

Rules:

* The store key carries the caller's MEHO tenant id, and the store checks
  the caller, so a handle of another tenant or another person is a miss.
* A miss (unknown, expired, another caller) fails with one clear message:
  a stored result lasts one hour by default, so the fix is to run the
  inventory read again.
* ``schema_version`` is checked before the rest, so an inventory of a
  newer shape is refused by name and not read wrongly.
* Merging keeps the handle order. A record whose ``source_id`` was seen
  before, or a server whose BIOS UUID or instance UUID was seen before, is
  kept in ``records`` but listed in ``duplicates``. The match is found by
  the same :class:`~.contracts.IdentityIndex` the source reads use, so
  ``same_as`` always names the first record of the machine, also through a
  chain of matches. A repeated record's identities still count, so a later
  record of the same machine from another source is found even when the
  first record had no BIOS UUID. The caller decides what a duplicate means
  for its output.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from pydantic import ValidationError

from meho_backplane.connectors.migration.contracts import (
    INVENTORY_SCHEMA_VERSION,
    IdentityIndex,
    IdentityMatch,
    InventoryEnvelope,
    InventoryRecord,
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
    match: Literal["source_id"] | IdentityMatch


@dataclass(frozen=True)
class LoadedInventory:
    """The inventories of all handles, in handle order, and their merged records."""

    handle_ids: list[str]
    envelopes: list[InventoryEnvelope]
    records: list[InventoryRecord] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)


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
    document: Any = stored.document
    if not isinstance(document, dict):
        raise InventoryLoadError(f"handle {handle_id} does not hold an inventory")
    version = document.get("schema_version")
    if version != INVENTORY_SCHEMA_VERSION:
        raise InventoryLoadError(
            f"handle {handle_id} holds inventory schema version {version!r}; "
            f"this MEHO reads version {INVENTORY_SCHEMA_VERSION!r}"
        )
    try:
        return InventoryEnvelope.model_validate(document)
    except ValidationError as exc:
        raise InventoryLoadError(
            f"handle {handle_id} does not hold a valid inventory: {_validation_summary(exc)}"
        ) from None


def merge_inventories(
    envelopes: list[InventoryEnvelope],
) -> tuple[list[InventoryRecord], list[Duplicate]]:
    records: list[InventoryRecord] = []
    duplicates: list[Duplicate] = []
    by_source_id: set[str] = set()
    identities = IdentityIndex()
    for envelope in envelopes:
        for record in envelope.records:
            records.append(record)
            if record.source_id in by_source_id:
                duplicates.append(Duplicate(record.source_id, record.source_id, "source_id"))
            else:
                by_source_id.add(record.source_id)
            found = identities.add(record)
            if found is not None:
                duplicates.append(Duplicate(found.source_id, found.same_as, found.match))
    return records, duplicates


async def load_inventories(
    operator: Operator,
    handle_ids: list[str],
    *,
    store: ResultHandleStore | None = None,
) -> LoadedInventory:
    """Read every handle of *operator*'s tenant as an inventory and merge them.

    Raises :class:`InventoryLoadError` for the first handle that is not a
    UUID, is missing or expired, is not an inventory, or has another
    schema version. A handle given twice is read once.
    """
    if not handle_ids:
        raise InventoryLoadError("give at least one inventory handle")
    if len(handle_ids) > MAX_INVENTORY_HANDLES:
        raise InventoryLoadError(f"give at most {MAX_INVENTORY_HANDLES} inventory handles")
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
    records, duplicates = merge_inventories(envelopes)
    return LoadedInventory(
        handle_ids=[str(handle_id) for handle_id in ordered],
        envelopes=envelopes,
        records=records,
        duplicates=duplicates,
    )
