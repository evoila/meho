# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Show the ``starting_grants`` input only while ``AGENT_GRANT_REQUIRED`` is not ``off``.

``meho_agent_principals_register``, ``meho_agents_create`` and
``meho_agents_edit`` take the pick of an agent's first grants
(:mod:`meho_backplane.agents.grant_sets`). The switch is ``off`` by
default. While it is ``off``, the tools show their
registered text and input unchanged, so an AI client sees the same tools as
before. While it is ``observe`` or ``on``, the tools get the
``starting_grants`` input and one more sentence that says when it is needed.

So with ``off`` a call that sends ``starting_grants`` is refused, while
REST and the console take it. This is on purpose: the ``tools/call``
input check uses the same tool that ``tools/list`` shows
(:attr:`~meho_backplane.mcp.registry.ToolDefinition.for_settings`), so
accepting the field with ``off`` would mean showing it with ``off``.
"""

from __future__ import annotations

from collections.abc import Callable

from meho_backplane.agents.grant_sets import STARTING_GRANTS_JSON_SCHEMA
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.registry import ToolDefinition
from meho_backplane.settings import get_settings

__all__ = ["with_starting_grants"]


def with_starting_grants(sentence: str) -> Callable[[ToolDefinition, Operator], ToolDefinition]:
    """Return a ``for_settings`` function that adds the pick while the switch is not off."""

    def for_settings(definition: ToolDefinition, operator: Operator) -> ToolDefinition:
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
