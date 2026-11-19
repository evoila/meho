# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Call hints on each ``search_operations`` hit, behind a switch (#4331).

A ranked hit names an operation, but not how to call it. An agent learns
the parameters from the description text, from exact-op mode, or from an
``invalid_params`` error after a wrong call. A small model often takes the
wrong call.

With the switch ``SEARCH_HIT_CALL_HINTS``
(:attr:`~meho_backplane.settings.Settings.search_hit_call_hints`, off by
default) each hit also carries two small facts:

* ``required_params`` -- the names in the top-level ``required`` list of
  the operation's stored ``parameter_schema``, in that order. Names only,
  no schemas: the full schema stays in exact-op mode.
* ``target_required`` -- whether a call needs a ``target``. It uses the
  dispatcher's own rule
  (:func:`~meho_backplane.operations.dispatcher.descriptor_requires_target`),
  so a hit never says "no target" for an operation that then asks for one.

With the switch off, :func:`hidden_call_hint_fields` keeps both fields out
of the answer, so it stays exactly as it was.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.settings import get_settings

__all__ = [
    "CALL_HINT_FIELDS",
    "hidden_call_hint_fields",
    "required_param_names",
    "search_hit_call_hints",
]

#: The two hit fields the switch adds.
CALL_HINT_FIELDS: Final[frozenset[str]] = frozenset({"required_params", "target_required"})


def search_hit_call_hints() -> bool:
    """Return the state of the ``SEARCH_HIT_CALL_HINTS`` switch."""
    return get_settings().search_hit_call_hints


def hidden_call_hint_fields() -> set[str]:
    """The hit fields to leave out: both of them while the switch is off."""
    return set() if search_hit_call_hints() else set(CALL_HINT_FIELDS)


def required_param_names(parameter_schema: Any) -> list[str]:
    """Return the top-level ``required`` names of *parameter_schema*, in order.

    The dispatcher checks ``params`` against this same stored schema, so
    these are the names a call must carry. A schema that is not an object,
    or has no ``required`` list, gives ``[]``. Entries that are not strings
    are skipped, and a name listed twice appears once.
    """
    if not isinstance(parameter_schema, dict):
        return []
    required = parameter_schema.get("required")
    if not isinstance(required, list):
        return []
    return list(dict.fromkeys(name for name in required if isinstance(name, str)))
