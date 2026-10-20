# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Fail-closed query-string redaction for the flight-recorder engine (#3841).

The vendor-call seam strips the query string from a recorded URL
(:func:`meho_backplane.flight_recorder._capture_util.safe_url`), because a
query value can carry a token or signed-URL material. Some transports still
need the query on the span: a Kubernetes server-side apply is only provable as
a dry run (or not) from its ``dryRun`` parameter.

:func:`redact_query` keeps every parameter **name** and keeps a parameter's
**value** only when the caller lists the name as safe. Every other value is
replaced by :data:`QUERY_VALUE_MARKER` without being read. This is an
allowlist, for the same reason the header filter is one: an unknown parameter
fails closed. A kept value still passes the credential-shape scrub.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

from meho_backplane.redaction.flight_recorder._content import scrub_content
from meho_backplane.redaction.flight_recorder.verdict import RedactionOutcome

__all__ = ["QUERY_VALUE_MARKER", "redact_query"]

#: Stands in for a query value whose parameter name is not on the safe list.
QUERY_VALUE_MARKER: Final[str] = "[redacted]"


def redact_query(params: Any, safe_names: Iterable[str]) -> RedactionOutcome:
    """Return the query as one ``name=value&...`` string, redacted.

    *params* is a mapping or a sequence of ``(name, value)`` pairs (the shape
    an SDK hands its HTTP layer). ``None`` or empty gives ``value=None``.
    A shape the engine cannot walk records nothing and is ``uncertain``, so
    the trace degrades to operator-only (F5).
    """
    if not params:
        return RedactionOutcome(value=None)
    try:
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params)
        safe = frozenset(safe_names)
        parts: list[str] = []
        for name, value in pairs:
            key = str(name)
            if key in safe and isinstance(value, (str, int, float, bool)):
                scrubbed, _ = scrub_content(str(value))
                parts.append(f"{key}={scrubbed}")
            else:
                parts.append(f"{key}={QUERY_VALUE_MARKER}")
    except Exception:
        return RedactionOutcome(
            value=None,
            uncertain=True,
            reasons=("query is not a list of name/value pairs: dropped (fail-closed)",),
        )
    return RedactionOutcome(value="&".join(parts))
