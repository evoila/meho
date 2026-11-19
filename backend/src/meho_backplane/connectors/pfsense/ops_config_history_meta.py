# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Op metadata for ``pfsense.config.history``.

The handler and the parsers are in
:mod:`meho_backplane.connectors.pfsense.ops_config_history`.
"""

from __future__ import annotations

from typing import Any

from meho_backplane.connectors.pfsense.ops import PfSenseOp
from meho_backplane.connectors.pfsense.redaction import REDACTED

__all__ = ["CONFIG_HISTORY_OPS"]

# ---------------------------------------------------------------------------
# Op metadata
# ---------------------------------------------------------------------------

_WHEN_TO_USE = (
    "Use to find out who changed the firewall configuration, when, and what "
    "changed, including changes made on the pfSense web page or shell "
    "(``pfsense.config.history``). Call it without ``revision`` to list the kept "
    "revisions, newest first (time, user, description, size); ``since`` keeps only "
    "revisions from that unix time on. Then call it with one revision's ``time`` "
    "to see what that revision changed against the one before it. "
    "Do not use ``pfsense.config.show`` for this: it returns only the live file, "
    "with no older revisions to compare."
)

_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "time": {"type": "integer"},
        "time_utc": {"type": ["string", "null"]},
        "user": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "size": {"type": ["integer", "null"]},
        "current": {"type": "boolean"},
    },
    "required": ["time", "time_utc", "user", "description", "size", "current"],
    "additionalProperties": False,
}

_SECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "section": {"type": "string"},
        "added": {"type": "integer"},
        "removed": {"type": "integer"},
        "only_hidden_values_changed": {"type": "boolean"},
    },
    "required": ["section", "added", "removed", "only_hidden_values_changed"],
    "additionalProperties": False,
}

CONFIG_HISTORY_OPS: tuple[PfSenseOp, ...] = (
    PfSenseOp(
        op_id="pfsense.config.history",
        handler_attr="config_history",
        summary="List the pfSense configuration change history, or show what one revision changed.",
        description=(
            "Lists the kept config revisions, or with ``revision`` shows what one "
            "changed. Known secrets are removed as in ``pfsense.config.show``; free "
            "text can still hold a secret someone typed in, also in older revisions "
            "after it was removed from the live file. ``user`` holds the admin's "
            "source address."
        ),
        parameter_schema={
            "type": "object",
            "properties": {
                "revision": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "The ``time`` of one revision from the list. Returns what it "
                        "changed against the revision before it."
                    ),
                },
                "section": {
                    "type": "string",
                    "description": (
                        "Only with ``revision``: the name of one changed section "
                        "(for example ``filter``). Returns the diff of that section."
                    ),
                },
                "since": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "List only: keep revisions from this unix time on.",
                },
            },
            "additionalProperties": False,
            "examples": [{}, {"since": 1763000000}, {"revision": 1763200000, "section": "filter"}],
        },
        response_schema={
            "type": "object",
            "properties": {
                "rows": {"type": "array", "items": _ROW_SCHEMA},
                "total": {"type": "integer"},
                "kept": {"type": "integer"},
                "backup_count": {"type": "integer"},
                "backup_count_is_default": {"type": "boolean"},
                "revision": {"type": "integer"},
                "revision_utc": {"type": ["string", "null"]},
                "user": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
                "previous_revision": {"type": ["integer", "null"]},
                "changed_sections": {"type": "array", "items": _SECTION_SCHEMA},
                "changed_total": {"type": "integer"},
                "diff": {"type": ["string", "null"]},
                "diff_omitted": {"type": "boolean"},
                "note": {"type": "string"},
            },
            "additionalProperties": True,
        },
        group_key="config",
        tags=("read-only", "config", "history", "pfsense"),
        safety_level="safe",
        requires_approval=False,
        llm_instructions={
            "when_to_use": _WHEN_TO_USE,
            "when_to_call": _WHEN_TO_USE,
            "next_step": (
                "From the list, call it again with the ``time`` of one row as "
                "``revision`` to see what that revision changed. If the answer says "
                "``diff_omitted``, call it again with ``section`` set to one changed "
                "section. To read one changed rule or user in full, use "
                "``pfsense.user.list`` or ``pfsense.route.static.list``."
            ),
            "parameter_hints": {
                "revision": "The ``time`` of a row from the list (unix time).",
                "section": "Only with ``revision``: a changed section name, e.g. ``filter``.",
                "since": "List only: unix time; keep revisions from then on.",
            },
            "output_shape": (
                "Without ``revision``: ``{rows: [{time, time_utc, user, description, "
                "size, current}], total, kept, backup_count, backup_count_is_default}``. "
                "``total`` counts the rows after ``since``; ``kept`` counts all revisions "
                "found. ``current`` is true for the live configuration. "
                "With ``revision``: ``{revision, revision_utc, user, description, "
                "previous_revision, changed_sections: [{section, added, removed, "
                "only_hidden_values_changed}], changed_total, diff, diff_omitted, note}``. "
                "``diff`` is a unified diff of the changed sections, or ``null`` when it is "
                "too long (``diff_omitted: true``): ask again with ``section``. "
                "``user`` and ``description`` are the text pfSense stored; ``user`` holds "
                "the admin's name and source address (for example "
                "``admin@<address> (Local Database)``). A value with a known secret shape "
                f"is replaced with ``{REDACTED}``. In ``diff``, known secret fields and "
                "known secret shapes are removed; free-text fields (descriptions, notes, "
                "cron or shell commands, custom config text) can still hold a secret that "
                "someone typed in, also in older revisions after it was removed from the "
                "live file, so treat the output as sensitive. A failed read or a "
                "file that cannot be parsed fails the call with an error that holds no file "
                "content. A long list comes back as a JSONFlux handle; read more with "
                "``result_query``."
            ),
        },
    ),
)
