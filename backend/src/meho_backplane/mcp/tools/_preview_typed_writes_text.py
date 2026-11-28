# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``preview_operation`` text about typed writes, and its switch (#5074).

With the ``PREVIEW_TYPED_WRITES`` setting on, ``preview_operation`` also
previews a typed write that does not carry ``requires_approval``, and a
credential write that has its own safe preview
(:func:`~meho_backplane.operations._request_preview._is_previewable`). The
tool text says so only while the switch is on. Off (the default), the tool
shows its registered text, unchanged.

With ``K8S_APPLY_PREVIEW_DRY_RUN`` on, every text the agent reads for this
tool says that the preview of ``k8s.apply`` sends a dry run to the cluster:
the full text (:data:`K8S_APPLY_DRY_RUN_TEXT_SWAPS`), the short text
(:data:`K8S_APPLY_DRY_RUN_SHORT_SWAP`), the output schema
(:data:`K8S_APPLY_DRY_RUN_SCHEMA_TEXTS`) and the text of the hosted agent
(:func:`k8s_apply_dry_run_description`). Off, each text is the registered
one.

:func:`preview_tool_for_settings` is the tool's ``for_settings`` function.
It runs the team-knowledge step and the policy fields step
(:func:`~meho_backplane.mcp.tools._preview_policy_fields.policy_schema_for_settings`)
first, then swaps the typed-write parts of
the description, then applies the short text when the session sees short
texts (:func:`~meho_backplane.mcp.short_texts.short_texts_shown`; the short
text that matches this switch).
"""

from __future__ import annotations

import copy
from typing import Any, Final

from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.mcp.short_texts import ShortText, apply_short_text, short_texts_shown
from meho_backplane.mcp.tools._operation_recall import recall_schema_for_settings
from meho_backplane.mcp.tools._preview_policy_fields import policy_schema_for_settings
from meho_backplane.mcp.tools._short_tool_texts import PREVIEW_OPERATION_SHORT
from meho_backplane.settings import get_settings

__all__ = [
    "K8S_APPLY_DRY_RUN_SCHEMA_TEXTS",
    "K8S_APPLY_DRY_RUN_SHORT_SWAP",
    "K8S_APPLY_DRY_RUN_TEXT_SWAPS",
    "PREVIEW_OPERATION_TYPED_WRITES_SHORT",
    "TYPED_WRITE_TEXT_SWAPS",
    "k8s_apply_dry_run_description",
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

#: The parts of the registered description that change while
#: ``K8S_APPLY_PREVIEW_DRY_RUN`` is on, and the texts that replace them.
K8S_APPLY_DRY_RUN_TEXT_SWAPS: Final[tuple[tuple[str, str], ...]] = (
    (
        "Inspection only -- it never sends the request and never re-dispatches a past one. ",
        "Inspection only -- it never runs the call and never re-dispatches "
        "a past one; for `k8s.apply` it sends a dry run to the cluster (see "
        "below). ",
    ),
    (
        "when the plan can be built without contacting the target; ",
        "when the plan can be built without contacting the target, or for "
        "`k8s.apply` from a dry run on the cluster: MEHO sends `dryRun=All` "
        "on every request that can change something and otherwise only reads "
        "the cluster's API list, so nothing is stored. Each document says "
        "`create`, `change`, `refused` or `not_checked`; a document that was "
        "not checked makes `plan_shown` false next to the plan, with a "
        "`plan_note`. A long list of documents comes back as a `handle` for "
        "`result_query`. In every other case ",
    ),
)

#: The sentence of the short text that changes while the switch is on.
K8S_APPLY_DRY_RUN_SHORT_SWAP: Final[tuple[str, str]] = (
    "Nothing is sent or written.\n",
    "Nothing is written on the target. For `k8s.apply`, MEHO sends a dry run "
    "to the cluster (`dryRun=All` on every request that can change "
    "something; nothing is stored) and shows `create`, `change`, `refused` "
    "or `not_checked` for each document.\n",
)

#: The output schema texts while the switch is on, by field.
K8S_APPLY_DRY_RUN_SCHEMA_TEXTS: Final[dict[str, str]] = {
    "proposed_effect": (
        "The plan of what the operation will change, the same block the "
        "approval request shows. Present on a typed/composite preview whose "
        "plan can be built without contacting the target, and on a preview "
        "of `k8s.apply` whose dry run on the target answered."
    ),
    "plan_shown": (
        "Typed/composite preview only: true when the whole plan is in "
        "`proposed_effect`. False with no `proposed_effect`: the plan is "
        "shown only on the approval request. False next to a "
        "`proposed_effect`: the dry run did not check every part (see "
        "`plan_note` and `proposed_effect.preview.not_checked`)."
    ),
    "plan_note": ("Plain reason the plan is not shown, or not whole (when plan_shown is false)."),
}


def k8s_apply_dry_run_description(description: str) -> str:
    """*description* with the parts about the dry run of ``k8s.apply`` swapped in."""
    for old, new in (*K8S_APPLY_DRY_RUN_TEXT_SWAPS, K8S_APPLY_DRY_RUN_SHORT_SWAP):
        description = description.replace(old, new)
    return description


def _with_k8s_apply_dry_run_schema(definition: ToolDefinition) -> ToolDefinition:
    """*definition* with the output schema texts of :data:`K8S_APPLY_DRY_RUN_SCHEMA_TEXTS`."""
    schema: dict[str, Any] | None = copy.deepcopy(definition.outputSchema)
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return definition
    for name, text in K8S_APPLY_DRY_RUN_SCHEMA_TEXTS.items():
        field = properties.get(name)
        if isinstance(field, dict):
            field["description"] = text
    return definition.model_copy(update={"outputSchema": schema})


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
    dry_run = settings.k8s_apply_preview_dry_run
    if dry_run:
        shown = _with_k8s_apply_dry_run_schema(shown)
    if short_texts_shown(operator):
        short = (
            PREVIEW_OPERATION_TYPED_WRITES_SHORT
            if settings.preview_typed_writes
            else PREVIEW_OPERATION_SHORT
        )
        shown = apply_short_text(shown, short)
    if dry_run:
        shown = shown.model_copy(
            update={"description": k8s_apply_dry_run_description(shown.description)}
        )
    return shown
