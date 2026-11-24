# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The CSV file of an Azure Migrate import: the checks MEHO makes before it uploads.

:func:`prepare_csv` reads the ``csv`` parameter and returns a
:class:`PreparedCsv`: the bytes to upload, their SHA-256, their size and the
number of server rows. A result and an audit text name the file by these
three numbers, never by its content.

The checks are the ones Azure documents for a CSV import: up to 20,000 servers
per file, and the four required columns of Azure's template (``Server name``,
``Cores``, ``Memory (In MB)``, ``OS name``, each with or without its ``*``
mark). Azure still checks every row; its bad-row reasons come back in the
import result.

The file travels inline in the call. MCP refuses a whole request above
1 MiB, and JSON can make a text up to three times longer (``\u00e9`` for
``é``). So MEHO takes at most :data:`MAX_CSV_BYTES` and refuses control
characters other than tab, carriage return and line feed (JSON writes each
of them as six bytes). A larger file is split over several calls.
"""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["MAX_CSV_BYTES", "MAX_CSV_ROWS", "REPEAT_KEY_PATTERN", "PreparedCsv", "prepare_csv"]

#: Azure's limit: servers per file and per project.
MAX_CSV_ROWS: Final[int] = 20_000

#: The largest file MEHO uploads. Three times this, plus the rest of the call,
#: stays below the 1 MiB request limit of MCP.
MAX_CSV_BYTES: Final[int] = 320 * 1024

#: What a ``repeat_key`` may hold. It adds a part to the tag name of :attr:`PreparedCsv.tag_key`.
REPEAT_KEY_PATTERN: Final[str] = r"^[A-Za-z0-9._-]{1,64}$"

#: The four required columns of Azure's template, in lower case (a ``*`` marks them there).
_REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("server name", "cores", "memory (in mb)", "os name")

#: The byte order mark some tools write at the start of a UTF-8 file.
_BOM: Final[str] = "\ufeff"

#: Control characters a CSV file may hold.
_ALLOWED_CONTROLS: Final[frozenset[str]] = frozenset("\t\r\n")


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
    return name.strip().removeprefix("*").strip().casefold()


def _has_control(value: str) -> bool:
    return any(
        (ord(char) < 0x20 or 0x7F <= ord(char) < 0xA0) and char not in _ALLOWED_CONTROLS
        for char in value
    )


def prepare_csv(value: object) -> PreparedCsv:
    """Check the ``csv`` parameter. Raises ``ValueError`` with a text that holds no row."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("csv must be the text of a CSV file with a header row.")
    if _has_control(value):
        raise ValueError(
            "csv must be text: it holds a control character other than tab or line end."
        )
    content = value.encode("utf-8")
    if len(content) > MAX_CSV_BYTES:
        raise ValueError(
            f"csv is {len(content):,} bytes. MEHO takes at most {MAX_CSV_BYTES:,} bytes in one "
            f"call. Split the file over several calls; Azure allows {MAX_CSV_ROWS:,} servers "
            "per project."
        )
    try:
        records = [
            row
            for row in csv.reader(io.StringIO(value.removeprefix(_BOM)))
            if any(c.strip() for c in row)
        ]
    except csv.Error as exc:
        raise ValueError(f"csv could not be read as CSV ({type(exc).__name__}).") from exc
    if not records:
        raise ValueError("csv must be the text of a CSV file with a header row.")
    header = [_column(cell) for cell in records[0]]
    missing = [name for name in _REQUIRED_COLUMNS if name not in header]
    if missing:
        raise ValueError(
            "csv has no column for: "
            + ", ".join(missing)
            + ". Azure's template names them Server name, Cores, Memory (In MB) and OS name."
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
