# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``preview_operation`` text about typed writes, and its switch (#5074).

With the ``PREVIEW_TYPED_WRITES`` setting on, ``preview_operation`` also
previews a typed write that does not carry ``requires_approval``, and a
credential write that has its own safe preview
(:func:`~meho_backplane.operations._request_preview._is_previewable`). The
tool text says so only while the switch is on. Off (the default), the tool
shows its registered text, unchanged.

:func:`preview_tool_for_settings` is the tool's ``for_settings`` function.
It runs the team-knowledge step first, then swaps the typed-write parts of
the description, then applies the short text when ``MCP_SHORT_TOOL_TEXTS``
is on (the short text that matches this switch).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.short_texts import ShortText, apply_short_text
from meho_backplane.mcp.tools._operation_recall import recall_schema_for_settings
from meho_backplane.mcp.tools._short_tool_texts import PREVIEW_OPERATION_SHORT
from meho_backplane.settings import get_settings

__all__ = [
    "PREVIEW_OPERATION_TYPED_WRITES_SHORT",
    "TYPED_WRITE_TEXT_SWAPS",
    "preview_tool_for_settings",
]

#: Each part of the registered description that changes with the switch on,
#: and the text that replaces it.
TYPED_WRITE_TEXT_SWAPS: Final[tuple[tuple[str, str], ...]] = (
    (
        "except a destructive or approval-gated one: it returns ",
        "except a write: it returns ",
    ),
    (
        "An approval-gated op that writes a credential (for example "
        '`vault.kv.put`) is NOT previewable and stays `status="unavailable"`, '
        "so its secret params never surface. ",
        "A credential write (for example `keycloak.user.reset_password`) with "
        'its own safe preview returns `status="ok"` with `proposed_effect` and '
        "no params copy: a secret value never shows. A credential write with "
        'no safe preview of its own stays `status="unavailable"`. A read-only '
        'typed op stays `status="unavailable"`. ',
    ),
)

PREVIEW_OPERATION_TYPED_WRITES_SHORT: Final = ShortText(
    description=PREVIEW_OPERATION_SHORT.description.replace(
        '`status="unavailable"`, except a destructive or approval-gated one: ',
        '`status="unavailable"`, except a write: ',
    ).replace(
        "A credential write cannot be previewed.",
        "A credential write shows its plan only, never the params, and only "
        "when it has its own safe preview.",
    ),
    inputs=PREVIEW_OPERATION_SHORT.inputs,
)


def preview_tool_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return ``preview_operation`` as it is shown under the current settings."""
    shown = recall_schema_for_settings(definition)
    settings = get_settings()
    if settings.preview_typed_writes:
        description = shown.description
        for old, new in TYPED_WRITE_TEXT_SWAPS:
            description = description.replace(old, new)
        shown = shown.model_copy(update={"description": description})
    if not settings.mcp_short_tool_texts:
        return shown
    short = (
        PREVIEW_OPERATION_TYPED_WRITES_SHORT
        if settings.preview_typed_writes
        else PREVIEW_OPERATION_SHORT
    )
    return apply_short_text(shown, short)
