# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The text of the three starting grant sets is true in every switch state.

The same text is in the ``set`` field of the REST schema and in the
``starting_grants`` input of the admin MCP tools.
"""

from __future__ import annotations

from meho_backplane.agents.grant_sets import (
    STARTING_GRANT_SETS_TEXT,
    STARTING_GRANTS_JSON_SCHEMA,
    StartingGrants,
)

_TEXT = (
    "'none': MEHO writes no grant. With AGENT_GRANT_REQUIRED='on' the agent then "
    "runs nothing until an admin adds a grant; with 'off' or 'observe' it can "
    "still run safe operations at once. "
    "'read-everything': safe operations run at once on every target; "
    "everything else waits for a person's approval. "
    "'read-targets': one grant for each target in 'targets'; safe operations "
    "run at once there. Only with 'on' does this keep the agent off other targets."
)


def test_the_set_text_is_pinned() -> None:
    assert STARTING_GRANT_SETS_TEXT == _TEXT


def test_the_rest_schema_uses_the_text() -> None:
    schema = StartingGrants.model_json_schema()
    assert schema["properties"]["set"]["description"] == _TEXT


def test_the_mcp_schema_uses_the_text() -> None:
    description = STARTING_GRANTS_JSON_SCHEMA["description"]
    assert isinstance(description, str)
    assert description.endswith(_TEXT)
