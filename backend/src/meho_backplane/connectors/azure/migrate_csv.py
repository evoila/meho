# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CSV file of an Azure Migrate import: the checks MEHO makes before it uploads.

:func:`prepare_csv` reads the ``csv`` parameter and returns a
:class:`PreparedCsv`: the bytes to upload, their SHA-256, their size and the
number of server rows. A result and an audit text name the file by these
three numbers, never by its content.

The checks are the ones Azure documents for a CSV import: up to 20,000 servers
per file, and the four required columns (server name, cores, memory in MB,
OS name). Azure still checks every row; its bad-row reasons come back in the
import result.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["MAX_CSV_BYTES", "MAX_CSV_ROWS", "PreparedCsv", "prepare_csv"]

#: Azure's limit: servers per file and per project.
MAX_CSV_ROWS: Final[int] = 20_000

#: The largest file MEHO uploads (20,000 rows of about 400 bytes).
MAX_CSV_BYTES: Final[int] = 8 * 1024 * 1024

#: The first words of the four required columns (a ``*`` marks them in Azure's template).
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("server name", "cores", "memory", "os name")


@dataclass(frozen=True)
class PreparedCsv:
    """A checked CSV file: its bytes, SHA-256, size and row count (without the header)."""

    content: bytes
    sha256: str
    size: int
    rows: int

    def describe(self) -> dict[str, Any]:
        """What a result says about the file. Never the file itself."""
        return {"sha256": self.sha256, "bytes": self.size, "rows": self.rows}

    @property
    def tag_key(self) -> str:
        """The site tag that records this file (the first 16 characters of its SHA-256)."""
        return f"meho-csv-{self.sha256[:16]}"


def _column(name: str) -> str:
    return name.strip().lstrip("*").strip().casefold()


def prepare_csv(value: object) -> PreparedCsv:
    """Check the ``csv`` parameter. Raises ``ValueError`` with a text that holds no row."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("csv must be the text of a CSV file with a header row.")
    if "\x00" in value:
        raise ValueError("csv must be text: it holds a NUL character.")
    content = value.encode("utf-8")
    if len(content) > MAX_CSV_BYTES:
        raise ValueError(
            f"csv is {len(content):,} bytes. MEHO uploads at most {MAX_CSV_BYTES:,} bytes. "
            f"Split the file; Azure allows {MAX_CSV_ROWS:,} servers per file and project."
        )
    try:
        records = [row for row in csv.reader(io.StringIO(value)) if any(c.strip() for c in row)]
    except csv.Error as exc:
        raise ValueError(f"csv could not be read as CSV ({type(exc).__name__}).") from exc
    if not records:
        raise ValueError("csv must be the text of a CSV file with a header row.")
    header = [_column(cell) for cell in records[0]]
    missing = [name for name in _REQUIRED_COLUMNS if not any(c.startswith(name) for c in header)]
    if missing:
        raise ValueError(
            "csv has no column for: "
            + ", ".join(missing)
            + ". Azure needs the server name, cores, memory (in MB) and OS name."
        )
    rows = len(records) - 1
    if rows < 1:
        raise ValueError("csv has a header row but no server rows.")
    if rows > MAX_CSV_ROWS:
        raise ValueError(
            f"csv has {rows:,} server rows. Azure allows {MAX_CSV_ROWS:,} per file and "
            "per project. Split the file."
        )
    return PreparedCsv(content, hashlib.sha256(content).hexdigest(), len(content), rows)
