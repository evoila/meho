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
It runs the team-knowledge step and the policy fields step
(:func:`~meho_backplane.mcp.tools._preview_policy_fields.policy_schema_for_settings`)
first, then swaps the typed-write parts of
the description, then applies the short text when the session sees short
texts (:func:`~meho_backplane.mcp.short_texts.short_texts_shown`; the short
text that matches this switch).
"""

from __future__ import annotations

from typing import Final

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.short_texts import ShortText, apply_short_text, short_texts_shown
from meho_backplane.mcp.tools._operation_recall import recall_schema_for_settings
from meho_backplane.mcp.tools._preview_policy_fields import policy_schema_for_settings
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
        "An approval-gated typed/composite op that writes a credential and is not "
        "destructive (for example `vault.kv.put`) is NOT previewable and stays "
        '`status="unavailable"`, so its secret params never surface. ',
        "A typed/composite credential write (for example `keycloak.user.reset_password`) with "
        'its own safe preview returns `status="ok"` with a `preview_hash` and '
        "no params copy: a secret value never shows. It shows `proposed_effect` only "
        "when that preview builds a plan; otherwise `plan_shown` is false. A "
        "typed/composite credential "
        'write with no safe preview of its own stays `status="unavailable"`, unless '
        'it is destructive. A read-only typed op stays `status="unavailable"`. ',
    ),
)

PREVIEW_OPERATION_TYPED_WRITES_SHORT: Final = ShortText(
    description=PREVIEW_OPERATION_SHORT.description.replace(
        '`status="unavailable"`, except a destructive or approval-gated one: ',
        '`status="unavailable"`, except a write: ',
    ).replace(
        'A typed one stays `status="unavailable"`, unless it is destructive: '
        "then it shows the redacted params and a `preview_hash`.",
        "A typed one with its own safe preview shows a `preview_hash`, never "
        "the params, and its plan when that preview builds one (if not, "
        "`plan_shown` is false). Without one, a destructive one shows "
        "the redacted params and a `preview_hash`; any other stays "
        '`status="unavailable"`.',
    ),
    inputs=PREVIEW_OPERATION_SHORT.inputs,
)


def preview_tool_for_settings(definition: ToolDefinition, operator: Operator) -> ToolDefinition:
    """Return ``preview_operation`` as *operator*'s session sees it under the current settings."""
    shown = policy_schema_for_settings(recall_schema_for_settings(definition))
    settings = get_settings()
    if settings.preview_typed_writes:
        description = shown.description
        for old, new in TYPED_WRITE_TEXT_SWAPS:
            description = description.replace(old, new)
        shown = shown.model_copy(update={"description": description})
    if not short_texts_shown(operator):
        return shown
    short = (
        PREVIEW_OPERATION_TYPED_WRITES_SHORT
        if settings.preview_typed_writes
        else PREVIEW_OPERATION_SHORT
    )
    return apply_short_text(shown, short)
