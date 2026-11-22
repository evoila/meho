# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Record whose login a secret store accepted for one call.

When MEHO reads a system's login from a secret store, the store checks one
login. This module keeps that login's id for the audit row of the call:

* The dispatcher opens a :func:`track_secret_read_as` scope for each call.
* The secret-store code calls :func:`record_secret_read_as` (or
  :func:`record_caller_secret_read`) after a secret read passed. A login that
  passed, followed by a refused read or by no read at all, records nothing.
* The audit writer reads :func:`current_secret_read_as` when it writes the row.

The value is the login's id only. It is never a token, a store path or a store
address. A call that reads no secret leaves it empty.

The scope holds a small mutable box, not the id itself. A value that code sets
in a copied context (a task or a worker thread) still reaches the box that the
audit writer reads.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "MEHO_OWN_IDENTITY",
    "current_secret_read_as",
    "record_caller_secret_read",
    "record_secret_read_as",
    "token_subject",
    "track_secret_read_as",
]

#: The fixed text for a read that the store saw under MEHO's own identity.
MEHO_OWN_IDENTITY = "meho"

#: Longest id kept on a row. A login id is short; this only stops an odd value.
_MAX_ID_CHARS = 255


@dataclass
class _Box:
    """The login id of the current call, once a store accepted one."""

    value: str | None = None


_box_var: ContextVar[_Box | None] = ContextVar("secret_read_as_box", default=None)


@contextmanager
def track_secret_read_as() -> Iterator[None]:
    """Open a fresh scope for one call.

    A step inside a multi-step call opens its own scope, so the row of the
    parent never takes the value of a step. The parent's scope is back when
    the step ends.
    """
    token = _box_var.set(_Box())
    try:
        yield
    finally:
        _box_var.reset(token)


def record_secret_read_as(login_id: str | None) -> None:
    """Note the login a store accepted for the current call.

    Does nothing outside a call scope, and for an empty id.
    """
    box = _box_var.get()
    if box is None or not isinstance(login_id, str):
        return
    # Keep printable characters only, so the database can always store the value.
    cleaned = "".join(ch for ch in login_id if ch.isprintable()).strip()[:_MAX_ID_CHARS]
    if cleaned:
        box.value = cleaned


def record_caller_secret_read(operator: Operator) -> None:
    """Note that a secret read passed under the login of *operator*'s token.

    Call it right after the store returned the secret, never after the login
    alone. The id is the ``sub`` of the token MEHO sent to the store.
    """
    record_secret_read_as(token_subject(operator.raw_jwt, operator.sub))


def current_secret_read_as() -> str | None:
    """Return the login id of the current call, or ``None`` when no store read passed."""
    box = _box_var.get()
    return box.value if box is not None else None


def token_subject(token: str, fallback: str) -> str:
    """Return the ``sub`` claim of the token MEHO sent to a store, else *fallback*.

    The token is the one MEHO has already checked, or minted for itself. Only
    the id is read here. No claim is trusted for a decision.
    """
    try:
        segment = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (IndexError, ValueError):
        return fallback
    sub = claims.get("sub") if isinstance(claims, dict) else None
    return sub if isinstance(sub, str) and sub else fallback
