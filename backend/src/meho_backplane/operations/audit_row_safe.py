# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Make the text of an audit row storable, and tell why a row was not stored.

The answer of a target can hold text that the ``audit_log`` table refuses: a
NUL character, a character that is not valid as text (a lone surrogate), or a
number that JSON cannot hold (NaN, Infinity). It can also hold a value that
JSON has no type for (a ``Decimal``, a date, a UUID, bytes, a set). The audit
writer runs :func:`storable_and_changed` over ``payload``, ``raw_payload`` and
``redaction_manifest`` before it stores a row, so the row is stored whatever
the target said. When a character or a number was replaced, the writer sets
:data:`TEXT_CHANGED` in ``payload``. The answer the caller gets is not
changed.

When the full row still cannot be stored, the writer stores a short row (see
:func:`short_row_payload`): the values that MEHO builds itself, and
:data:`FULL_ROW_LOST` with the error class. :func:`is_database_unreachable`
tells that case apart from "the database cannot be reached": there a short
row cannot be stored either, so the writer raises at once.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from typing import Any, Final

import sqlalchemy.exc as sa_exc
from prometheus_client import Counter

__all__ = [
    "AUDIT_FULL_ROW_LOST_TOTAL",
    "AUDIT_TEXT_CHANGED_TOTAL",
    "FULL_ROW_LOST",
    "INVALID_CHARACTER_MARKER",
    "MAX_DURATION_MS",
    "NUL_MARKER",
    "TEXT_CHANGED",
    "clamp_duration_ms",
    "is_database_unreachable",
    "make_storable",
    "short_row_payload",
    "storable_and_changed",
]

#: Key in ``payload`` of a short row. Its value is ``{"error_class": <name>}``.
FULL_ROW_LOST: Final[str] = "full_row_lost"

#: Key in ``payload`` set to ``True`` by the writer when it replaced a
#: character or a number of the row. Only the writer sets it.
TEXT_CHANGED: Final[str] = "text_changed"

#: What replaces a NUL character in a stored text.
NUL_MARKER: Final[str] = "<NUL>"

#: What replaces a character that is not valid as text (a lone surrogate).
INVALID_CHARACTER_MARKER: Final[str] = "<invalid character>"

#: The largest value of the ``numeric(10,2)`` column ``audit_log.duration_ms``.
MAX_DURATION_MS: Final[Decimal] = Decimal("99999999.99")

#: Audit rows stored as a short row because the full row was refused.
AUDIT_FULL_ROW_LOST_TOTAL: Counter = Counter(
    "meho_audit_full_row_lost_total",
    "Audit rows stored as a short row because the database refused the full row.",
    ["error_class"],
)

#: Audit rows stored with a replaced character or number (``text_changed``).
AUDIT_TEXT_CHANGED_TOTAL: Counter = Counter(
    "meho_audit_text_changed_total",
    "Audit rows stored with a character or a number replaced so the table can hold it.",
)

_SURROGATE: Final = re.compile("[\ud800-\udfff]")

#: SQLSTATE class of a connection fault: every code in it means "unreachable".
_CONNECTION_FAULT_CLASS: Final[str] = "08"

#: SQLSTATE codes outside class 08 that mean the database cannot be used now:
#: too many connections, a shutdown, a crash, "cannot connect now", the
#: database was dropped, the idle session was closed. Other codes of class 57
#: (57014, a cancelled statement or a statement timeout) are not here: a
#: short row can still be stored then.
_UNREACHABLE_SQLSTATES: Final[frozenset[str]] = frozenset(
    {"53300", "57P01", "57P02", "57P03", "57P04", "57P05"}
)

#: Keys of the full ``payload`` that a short row keeps. MEHO's own code
#: builds each of them (from the operation, the login, the call context or
#: the result handle), so none holds text of the target or of a handler.
#: Left out: ``error`` and the handler's extras.
_SHORT_ROW_KEYS: Final[tuple[str, ...]] = (
    "op_id",
    "op_class",
    "result_status",
    "params_hash",
    "source_kind",
    "connector_product",
    "connector_version",
    "connector_impl_id",
    "parent_audit_id",
    "redaction_policy_id",
    "handle_id",
    "total_rows",
    "sample_rows_returned",
    "agent_session_id",
    "agent_model",
    "agent_provider",
    "agent_cost",
    "run_id",
    "step_id",
    "reveal_secret",
    "caller_gone_at",
    "principal_name",
    "client",
)

#: Key of the approval request id in a short row and in an error's extras.
_APPROVAL_REQUEST_ID: Final[str] = "approval_request_id"


def _safe_text(value: str, changed: list[bool]) -> str:
    safe = _SURROGATE.sub(INVALID_CHARACTER_MARKER, value.replace("\x00", NUL_MARKER))
    if safe != value:
        changed[0] = True
    return safe


def _key_text(key: Any, changed: list[bool]) -> str:
    """The text form of a key; a non-text key is written as JSON writes it."""
    if isinstance(key, str):
        return _safe_text(key, changed)
    if key is None or isinstance(key, bool | int | float):
        # The form ``json.dumps`` gives a key of these types.
        return json.dumps(key)
    return _safe_text(str(key), changed)


def _storable_dict(value: dict[Any, Any], changed: list[bool]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, item in value.items():
        new_key = _key_text(key, changed)
        if new_key in out:
            changed[0] = True
            suffix = 2
            while f"{new_key}#{suffix}" in out:
                suffix += 1
            new_key = f"{new_key}#{suffix}"
        out[new_key] = _storable(item, changed)
    return out


def _storable_scalar(value: Any, changed: list[bool]) -> Any:
    """A value JSON has no type for, as text; any other value as it is."""
    if isinstance(value, Decimal | uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, bytes | bytearray):
        return _safe_text(bytes(value).decode("utf-8", "backslashreplace"), changed)
    return value


def _storable(value: Any, changed: list[bool]) -> Any:
    if isinstance(value, str):
        return _safe_text(value, changed)
    if isinstance(value, float):
        if isfinite(value):
            return value
        changed[0] = True
        return str(value)
    if isinstance(value, dict):
        return _storable_dict(value, changed)
    if isinstance(value, list | tuple):
        return [_storable(item, changed) for item in value]
    if isinstance(value, set | frozenset):
        return sorted((_storable(item, changed) for item in value), key=repr)
    return _storable_scalar(value, changed)


def storable_and_changed(value: Any) -> tuple[Any, bool]:
    """Return a storable copy of *value*, and whether a character or number was replaced.

    Keys and values are covered, at any depth:

    * A NUL character or a lone surrogate becomes a visible marker, and a
      NaN or Infinity becomes text. These count as a change.
    * A ``Decimal``, ``datetime``, ``date``, ``UUID``, ``bytes`` or ``set``
      becomes text (a set becomes a list). A key that is not text becomes
      its text form. These do not count as a change.
    * Two keys that become equal: the second gets a numbered suffix, so no
      entry is lost. This counts as a change.
    """
    changed = [False]
    return _storable(value, changed), changed[0]


def make_storable(value: Any) -> Any:
    """Return a copy of *value* whose every text and number the table can store."""
    return storable_and_changed(value)[0]


def clamp_duration_ms(duration_ms: float) -> Decimal:
    """*duration_ms* as the column stores it, cut to :data:`MAX_DURATION_MS`.

    A value that is not a finite number (NaN) is stored as the largest value.
    """
    if not isfinite(duration_ms):
        return MAX_DURATION_MS
    return min(Decimal(str(round(duration_ms, 2))), MAX_DURATION_MS)


def _approval_request_id(*sources: dict[str, Any] | None) -> str | None:
    """The first approval request id in *sources* that is a UUID.

    MEHO's code sets this id: on the result of a call that waits for
    approval or was refused, and on the run of an approved request. Only a
    UUID is kept, so no free text reaches the short row.
    """
    for source in sources:
        raw = (source or {}).get(_APPROVAL_REQUEST_ID)
        if raw is None:
            continue
        try:
            return str(uuid.UUID(str(raw)))
        except ValueError:
            continue
    return None


def short_row_payload(
    exc: BaseException,
    core: dict[str, Any],
    *,
    error_extras: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``payload`` of a short row, before :func:`storable_and_changed`.

    *core* is the payload as MEHO's own code built it, before the handler's
    extras and the error were added. The short row keeps its keys in
    :data:`_SHORT_ROW_KEYS`, the approval request id (from *error_extras*,
    else from the full *payload*; a UUID only), and :data:`FULL_ROW_LOST`
    with the error class.
    """
    short: dict[str, Any] = {key: core[key] for key in _SHORT_ROW_KEYS if key in core}
    approval_request_id = _approval_request_id(error_extras, payload)
    if approval_request_id is not None:
        short[_APPROVAL_REQUEST_ID] = approval_request_id
    short[FULL_ROW_LOST] = {"error_class": type(exc).__name__}
    return short


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
        return sqlstate.startswith(_CONNECTION_FAULT_CLASS) or sqlstate in _UNREACHABLE_SQLSTATES
    return False
