# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check, before a call is sent, that the audit row of the call can be stored.

For a call that runs at once, MEHO writes the audit row after the call to
the target. When that write fails, the target may already hold the change
and the caller gets ``audit_commit_failed`` (outcome unknown). Most of the
row is known before the call: who asked, the tenant, the operation, the
ticket id, the session and client fields and the hash of the parameters.
:func:`audit_row_problem` checks those values against the rules of the
``audit_log`` table, so a row that can never be stored is refused before
anything is sent to the target. Numbers that JSON cannot hold (NaN and
Infinity) are refused too. When the check itself fails (for example on a
payload nested too deep to walk), the call is refused as well.

The check looks at values only. It sends no statement to the database, so
it does not read the registered agent name, which is in the database.
What is known only after the call (the vendor answer in ``raw_payload``
and ``redaction_manifest``, the database being down) stays with
``audit_commit_failed``.

The column lists below say how each text and JSON column of ``audit_log``
is covered. A test fails when a new column is in none of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from math import isfinite
from typing import Any, Final

import structlog

from meho_backplane.auth.client_identity import apply_client_block
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import MACHINE_PRINCIPAL_KINDS, Operator
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations._audit import _build_audit_payload, step_id_var, work_ref_var

__all__ = [
    "AFTER_CALL_JSON_COLUMNS",
    "CHECKED_JSON_COLUMNS",
    "CHECKED_TEXT_COLUMNS",
    "FIXED_TEXT_COLUMNS",
    "INDEXED_TEXT_COLUMNS",
    "INDEX_TEXT_MAX_BYTES",
    "AuditRowProblem",
    "audit_row_problem",
]

_log = structlog.get_logger(__name__)

#: Text columns of ``audit_log`` that have an index. A btree entry may not
#: pass about 2700 bytes, so a longer value cannot be stored. 2600 bytes is a
#: safe upper bound: it keeps a margin under that limit, so every value the
#: check lets through fits the index.
INDEXED_TEXT_COLUMNS: Final[frozenset[str]] = frozenset({"operator_sub", "actor_sub", "work_ref"})
INDEX_TEXT_MAX_BYTES: Final[int] = 2600

#: Text columns whose value comes from the caller or the request, checked here.
CHECKED_TEXT_COLUMNS: Final[frozenset[str]] = INDEXED_TEXT_COLUMNS | {"path", "step_id"}

#: Text columns that only ever get a fixed value from the code (a constant, an
#: enum, or a value the table's CHECK rule limits). Nothing to check.
FIXED_TEXT_COLUMNS: Final[frozenset[str]] = frozenset({"method", "event_class", "policy_decision"})

#: JSON columns checked before the call: every text in them (keys and values).
CHECKED_JSON_COLUMNS: Final[frozenset[str]] = frozenset({"payload"})

#: JSON columns filled from the answer of the target, so they are not known
#: before the call.
AFTER_CALL_JSON_COLUMNS: Final[frozenset[str]] = frozenset({"raw_payload", "redaction_manifest"})

_BAD_CHARACTER: Final[str] = "holds a character that the database cannot store"
_TOO_LONG: Final[str] = "is too long for the database"
_NOT_A_JSON_NUMBER: Final[str] = "holds a number that JSON cannot hold (NaN or Infinity)"
_CANNOT_CHECK: Final[str] = "could not be checked"


@dataclass(frozen=True)
class AuditRowProblem:
    """The first field of the audit row that cannot be stored, and why.

    Never holds the value itself: it can be a secret or a login detail.
    """

    field: str
    reason: str


def _text_reason(value: str, *, indexed: bool) -> str | None:
    """Return why *value* cannot be stored in a text column, or ``None``."""
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return _BAD_CHARACTER
    if "\x00" in value:
        return _BAD_CHARACTER
    if indexed and len(encoded) > INDEX_TEXT_MAX_BYTES:
        return _TOO_LONG
    return None


def _value_reason(value: str | float) -> str | None:
    """Return why a text or number in the payload cannot be stored, or ``None``."""
    if isinstance(value, float):
        return None if isfinite(value) else _NOT_A_JSON_NUMBER
    return _text_reason(value, indexed=False)


def _values_in(value: Any, path: str) -> Iterator[tuple[str, str | float]]:
    """Yield ``(field, value)`` for every key, string and float inside *value*.

    The field is built from keys, so a key that cannot be stored is never put
    into it: that entry is named by its position, as ``<path>.<key N>``.
    Otherwise the refusal would hold the same bad text and could not be sent.
    """
    if isinstance(value, str | float):
        yield path, value
    elif isinstance(value, dict):
        for position, (key, item) in enumerate(value.items()):
            if isinstance(key, str) and _text_reason(key, indexed=False) is not None:
                key_path = f"{path}.<key {position}>"
                yield key_path, key
            else:
                key_path = f"{path}.{key}"
            yield from _values_in(item, key_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            yield from _values_in(item, f"{path}[{index}]")


def audit_row_problem(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    params_hash: str,
) -> AuditRowProblem | None:
    """Return the first problem that would stop the audit row, or ``None``.

    Reads the same values that :func:`~meho_backplane.operations._audit.write_audit_row`
    reads: the caller, the operation id, the ticket id and step id of the
    request, and the payload (built by the same function, with the client
    block and the caller's name).

    Never raises: when the check itself fails, the payload is named as the
    problem, so the call is refused rather than sent unchecked.
    """
    try:
        return _first_problem(operator=operator, descriptor=descriptor, params_hash=params_hash)
    except Exception as exc:
        _log.warning("audit_row_check_failed", error_type=type(exc).__name__)
        return AuditRowProblem("payload", _CANNOT_CHECK)


def _first_problem(
    *,
    operator: Operator,
    descriptor: EndpointDescriptor,
    params_hash: str,
) -> AuditRowProblem | None:
    columns: dict[str, str | None] = {
        "operator_sub": operator.sub,
        "actor_sub": resolve_actor_sub(),
        "path": descriptor.op_id,
        "step_id": step_id_var.get(),
        "work_ref": work_ref_var.get(),
    }
    for name, value in columns.items():
        if value is None:
            continue
        reason = _text_reason(value, indexed=name in INDEXED_TEXT_COLUMNS)
        if reason is not None:
            return AuditRowProblem(name, reason)
    payload = _build_audit_payload(descriptor, params_hash, "ok")
    apply_client_block(payload, operator)
    # The name that ``_apply_principal_name`` stores (``principal_display_name``):
    # the token's name, else the client id of a machine login. The registered
    # agent name needs the database, so only the token's own values are checked.
    principal_name = operator.name
    if not principal_name and operator.principal_kind in MACHINE_PRINCIPAL_KINDS:
        principal_name = operator.client_id
    if principal_name:
        payload["principal_name"] = principal_name
    for field, leaf in _values_in(payload, "payload"):
        reason = _value_reason(leaf)
        if reason is not None:
            return AuditRowProblem(field, reason)
    return None
