# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Show the ``starting_grants`` input only while ``AGENT_GRANT_REQUIRED`` is not ``off``.

``meho_agent_principals_register`` and ``meho_agents_create`` take the
pick of an agent's first grants (:mod:`meho_backplane.agents.grant_sets`).
The switch is ``off`` by default. While it is ``off``, both tools show their
registered text and input unchanged, so an AI client sees the same tools as
before. While it is ``observe`` or ``on``, the tools get the
``starting_grants`` input and one more sentence that says when it is needed.
"""

from __future__ import annotations

from collections.abc import Callable

from meho_backplane.agents.grant_sets import STARTING_GRANTS_JSON_SCHEMA
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = ["with_starting_grants"]


def with_starting_grants(sentence: str) -> Callable[[ToolDefinition], ToolDefinition]:
    """Return a ``for_settings`` function that adds the pick while the switch is not off."""

    def for_settings(definition: ToolDefinition) -> ToolDefinition:
        if get_settings().agent_grant_required == "off":
            return definition
        schema = definition.inputSchema
        properties = {**schema["properties"], "starting_grants": STARTING_GRANTS_JSON_SCHEMA}
        return definition.model_copy(
            update={
                "description": f"{definition.description} {sentence}",
                "inputSchema": {**schema, "properties": properties},
            }
        )

    return for_settings
