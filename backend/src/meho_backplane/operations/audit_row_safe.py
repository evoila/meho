# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make the text of an audit row storable, and tell why a row was not stored.

The answer of a target can hold text that the ``audit_log`` table refuses: a
NUL character, a character that is not valid as text (a lone surrogate), or a
number that JSON cannot hold (NaN, Infinity). The audit writer runs
:func:`make_storable` over ``payload``, ``raw_payload`` and
``redaction_manifest`` before it stores a row, so the row is stored whatever
the target said. The answer the caller gets is not changed.

When the full row still cannot be stored, the writer stores a short row that
holds :data:`FULL_ROW_LOST` and the error class (see
:func:`short_row_payload`). :func:`is_database_unreachable` tells that case
apart from "the database cannot be reached": there a short row cannot be
stored either, so the writer raises at once.
"""

from __future__ import annotations

import re
from math import isfinite
from typing import Any, Final

import sqlalchemy.exc as sa_exc

__all__ = [
    "FULL_ROW_LOST",
    "INVALID_CHARACTER_MARKER",
    "NUL_MARKER",
    "is_database_unreachable",
    "make_storable",
    "short_row_payload",
]

#: Key in ``payload`` of a short row. Its value is ``{"error_class": <name>}``.
FULL_ROW_LOST: Final[str] = "full_row_lost"

#: What replaces a NUL character in a stored text.
NUL_MARKER: Final[str] = "<NUL>"

#: What replaces a character that is not valid as text (a lone surrogate).
INVALID_CHARACTER_MARKER: Final[str] = "<invalid character>"

_SURROGATE: Final = re.compile("[\ud800-\udfff]")

#: SQLSTATE classes that mean the database cannot be used right now: a
#: connection fault (08), a shutdown or restart (57), a resource limit (53).
_UNREACHABLE_SQLSTATE_CLASSES: Final[tuple[str, ...]] = ("08", "53", "57")


def _safe_text(value: str) -> str:
    return _SURROGATE.sub(INVALID_CHARACTER_MARKER, value.replace("\x00", NUL_MARKER))


def make_storable(value: Any) -> Any:
    """Return a copy of *value* whose every text and number the table can store.

    Keys and values are covered, at any depth. Two keys that become equal
    after the change get a numbered suffix, so no entry is lost.
    """
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, float):
        return value if isfinite(value) else str(value)
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            new_key = _safe_text(key) if isinstance(key, str) else key
            if new_key in out:
                suffix = 2
                while f"{new_key}#{suffix}" in out:
                    suffix += 1
                new_key = f"{new_key}#{suffix}"
            out[new_key] = make_storable(item)
        return out
    if isinstance(value, list | tuple):
        return [make_storable(item) for item in value]
    return value


def short_row_payload(exc: BaseException) -> dict[str, Any]:
    """The ``payload`` of a short row: the mark and the error class, no text."""
    return {FULL_ROW_LOST: {"error_class": type(exc).__name__}}


def is_database_unreachable(exc: BaseException) -> bool:
    """``True`` when *exc* says the database cannot be reached or used.

    A short row cannot be stored then, so the writer does not try one.
    """
    if isinstance(exc, OSError | sa_exc.InterfaceError | sa_exc.TimeoutError):
        return True
    if isinstance(exc, sa_exc.DBAPIError):
        if exc.connection_invalidated or isinstance(exc, sa_exc.OperationalError):
            return True
        sqlstate = str(getattr(exc.orig, "sqlstate", None) or "")
        return sqlstate.startswith(_UNREACHABLE_SQLSTATE_CLASSES)
    return False
