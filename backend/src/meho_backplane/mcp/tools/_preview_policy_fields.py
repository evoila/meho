# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``preview_operation`` output fields ``policy_rule`` and ``approvers``, and their switch.

With the ``POLICY_ENGINE_SHADOW`` setting on, a preview whose op resolved
also carries the rule the policy evaluator matched and the login groups
that may approve (:func:`~meho_backplane.policy.who_approves.preview_policy_fields`).
Off (the default), the answer has neither field, and the ``outputSchema``
has neither field either (:func:`policy_schema_for_settings`), so an agent
reads nothing about fields that never come.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = ["PREVIEW_POLICY_OUTPUT_SCHEMA", "policy_schema_for_settings"]

#: The ``outputSchema`` entries of the two fields.
PREVIEW_POLICY_OUTPUT_SCHEMA: Final[dict[str, Any]] = {
    "policy_rule": {
        "type": ["object", "null"],
        "description": (
            "The policy rule that matched this call: `source`, `rule_id`, "
            "`text`, and `decided` (false: the rule is shown, but today's "
            "checks decide the call). Null when no rule could be worked out."
        ),
        "properties": {
            "source": {"type": "string"},
            "rule_id": {"type": "string"},
            "text": {"type": "string"},
            "decided": {"type": "boolean"},
        },
    },
    "approvers": {
        "type": ["array", "null"],
        "items": {"type": "string"},
        "description": (
            "The login groups that may approve this call when "
            "`policy_verdict` is `needs-approval`, for example "
            "`MEHO-Admins`. Empty when the call does not wait. "
            "Null when the stored policy is broken."
        ),
    },
}


def policy_schema_for_settings(definition: ToolDefinition) -> ToolDefinition:
    """Return the tool without the two output fields while ``POLICY_ENGINE_SHADOW`` is off."""
    if get_settings().policy_engine_shadow or definition.outputSchema is None:
        return definition
    schema = definition.outputSchema
    properties = {
        key: value
        for key, value in schema.get("properties", {}).items()
        if key not in PREVIEW_POLICY_OUTPUT_SCHEMA
    }
    return definition.model_copy(update={"outputSchema": {**schema, "properties": properties}})
