# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Bounded ``search_memory`` output: schema, envelope, and wire limit (#3751).

Helpers for :mod:`meho_backplane.mcp.tools.memory`. A search hit is short
evidence, not the entry: its snippet goes inside the untrusted-text
envelope, and the whole response stays inside the shared completed-call
wire limit from :mod:`meho_backplane.operations.delivery_budget`.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.memory.schemas import MemoryEntrySearchHit, MemoryScope
from meho_backplane.memory.snippet import SNIPPET_MAX_CHARS
from meho_backplane.operations.delivery_budget import measure_delivered_wire_bytes
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text

__all__ = ["SEARCH_OUTPUT_SCHEMA", "bounded_search_response", "hit_payload"]

#: Shown on a ``search_memory`` response that left out hits to stay
#: inside the delivery limit.
_PARTIAL_SEARCH_REMEDIATION: Final[str] = (
    "Some lower-ranked hits were left out to keep the response inside the "
    "delivery limit. Lower `limit` or use a narrower query to see them."
)

_NULLABLE_STRING: Final[dict[str, Any]] = {"type": ["string", "null"]}
_DATE_TIME: Final[dict[str, Any]] = {"type": "string", "format": "date-time"}

#: ``outputSchema`` of ``search_memory``. One bounded hit per entry.
SEARCH_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "hits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rank": {"type": "integer", "minimum": 1},
                    "id": {"type": "string", "format": "uuid"},
                    "scope": {"type": "string", "enum": [scope.value for scope in MemoryScope]},
                    "slug": {"type": "string"},
                    "user_sub": _NULLABLE_STRING,
                    "target_name": _NULLABLE_STRING,
                    "created_at": _DATE_TIME,
                    "updated_at": _DATE_TIME,
                    "expires_at": {"type": ["string", "null"], "format": "date-time"},
                    "expiry_state": {"type": "string", "enum": ["never", "scheduled"]},
                    "snippet": {"type": "string"},
                    "snippet_truncated": {"type": "boolean"},
                    "body_chars": {"type": "integer", "minimum": 0},
                    "locator": {"type": "string"},
                },
                "required": [
                    "rank",
                    "id",
                    "scope",
                    "slug",
                    "user_sub",
                    "target_name",
                    "created_at",
                    "updated_at",
                    "expires_at",
                    "expiry_state",
                    "snippet",
                    "snippet_truncated",
                    "body_chars",
                    "locator",
                ],
                "additionalProperties": False,
            },
        },
        "delivery": {"type": "string", "enum": ["complete", "partial"]},
        "omitted_hits": {"type": "integer", "minimum": 0},
        "snippet_max_chars": {"type": "integer", "minimum": 1},
        "remediation": {"type": "string"},
    },
    "required": ["hits", "delivery", "omitted_hits", "snippet_max_chars"],
    "additionalProperties": False,
}


def hit_payload(hit: MemoryEntrySearchHit) -> dict[str, Any]:
    """Serialise one hit, with its snippet inside the untrusted-text envelope.

    The snippet is agent-authored stored text re-served into an LLM
    context, so it is wrapped at this read boundary like the full body
    on the selected read.
    """
    data = hit.model_dump(mode="json")
    data["snippet"] = wrap_untrusted_text(data["snippet"])
    return data


def _search_response(hits: list[dict[str, Any]], omitted: int) -> dict[str, Any]:
    """Build the ``search_memory`` response for *hits*, *omitted* left out."""
    response: dict[str, Any] = {
        "hits": hits,
        "delivery": "partial" if omitted else "complete",
        "omitted_hits": omitted,
        "snippet_max_chars": SNIPPET_MAX_CHARS,
    }
    if omitted:
        response["remediation"] = _PARTIAL_SEARCH_REMEDIATION
    return response


def bounded_search_response(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep the response inside the completed-call wire limit (#3641).

    Measures the response the way the MCP encoder sends it (text block
    plus ``structuredContent``) against
    ``result_complete_response_wire_max_bytes``. While it is over, the
    lowest-ranked hit is left out and ``delivery`` becomes ``partial``.
    The limit's floor always fits the empty response.
    """
    max_bytes = get_settings().result_complete_response_wire_max_bytes
    kept = list(hits)
    response = _search_response(kept, 0)
    while kept and measure_delivered_wire_bytes(response) > max_bytes:
        kept.pop()
        response = _search_response(kept, len(hits) - len(kept))
    return response
