# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The file formats of ``inventory.export``: format name to mapping.

A format is a plain mapping from merged inventory records to the lines of
one file: its columns, units, name rules and size limit. Words of the tool
that imports the file stay on the format, never on the read. A new format
is one new mapping module, one entry here and one golden test file.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from meho_backplane.connectors.inventory import azure_migrate_csv
from meho_backplane.connectors.inventory.contracts import InventoryRecord
from meho_backplane.connectors.inventory.export import ExportResult
from meho_backplane.connectors.inventory.inventory_load import Duplicate

__all__ = [
    "EXPORT_FORMATS",
    "ExportFormat",
    "UnknownExportFormatError",
    "get_export_format",
]

Mapper = Callable[[Iterable[InventoryRecord], Iterable[Duplicate]], ExportResult]


@dataclass(frozen=True)
class ExportFormat:
    """One file format: its name, a short text for callers, and its mapping."""

    name: str
    description: str
    build: Mapper


class UnknownExportFormatError(ValueError):
    """The format name is not in :data:`EXPORT_FORMATS`. The message lists the known ones."""


EXPORT_FORMATS: Final[Mapping[str, ExportFormat]] = MappingProxyType(
    {
        azure_migrate_csv.FORMAT_NAME: ExportFormat(
            name=azure_migrate_csv.FORMAT_NAME,
            description=(
                "CSV file for the import in Azure Migrate: Microsoft's column names, "
                f"one line per server, at most {azure_migrate_csv.MAX_SERVERS_PER_FILE} "
                "servers, unique server names, CRLF line ends."
            ),
            build=azure_migrate_csv.inventory_to_csv,
        ),
    }
)


def get_export_format(name: str) -> ExportFormat:
    """Return the format called *name*, or fail with a message that lists the known formats."""
    found = EXPORT_FORMATS.get(name)
    if found is None:
        known = ", ".join(sorted(EXPORT_FORMATS))
        raise UnknownExportFormatError(f"unknown format {name!r}; known formats: {known}")
    return found
