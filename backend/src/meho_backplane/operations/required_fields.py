# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer fields that a connector always keeps when a long answer is shortened.

The result reducer turns a long answer into a short summary and a link to
the stored result. A connector can name top-level fields that the summary
must always carry whole, for every operation of its product
(:func:`register_required_fields`). The dispatcher passes the names to the
reducer directly. They are not part of an operation's instructions, so the
operation detail does not show them and an admin's edit of the instructions
does not remove them.

The connector gives a function, so the fields can depend on a switch. With
the switch off the function returns nothing, and the summary is as before.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

__all__ = ["REQUIRED_FIELDS_CONTEXT_KEY", "register_required_fields", "required_fields"]

#: The reducer context key with the names of the fields.
REQUIRED_FIELDS_CONTEXT_KEY: Final = "required_fields"

#: product -> the function that gives the field names.
_REQUIRED_FIELDS: dict[str, Callable[[], tuple[str, ...]]] = {}


def register_required_fields(product: str, fields: Callable[[], tuple[str, ...]]) -> None:
    """Keep the fields that *fields* gives in every answer of *product*. Safe to call twice."""
    _REQUIRED_FIELDS[product] = fields


def required_fields(product: str | None) -> tuple[str, ...]:
    """The fields every answer of *product* keeps now; empty for none."""
    fields = _REQUIRED_FIELDS.get(product) if product is not None else None
    return fields() if fields is not None else ()
