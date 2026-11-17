# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row of an agent definition create names the agent that gets the grants.

``starting_grant_agent`` is the definition's ``identity_ref``, the same
field an edit writes. REST and MCP are checked here; the console is in
``test_ui_agent_form_refusals.py``.

The helpers and the autouse fixtures come from ``test_agent_starting_grants.py``.
"""

from __future__ import annotations

import pytest
import structlog
from fastapi.testclient import TestClient

from meho_backplane.mcp.tools.agents import _create_handler
from tests.test_agent_starting_grants import (  # noqa: F401 -- the autouse fixtures apply by import
    _audit_rows,
    _definition,
    _operator,
    _post,
    _seed_principal,
    _seed_tenants,
    _settings_env,
    _stub_vault_write,
    _switch,
    client,
)


@pytest.mark.asyncio
async def test_rest_create_names_the_agent_that_gets_the_grants(
    client: TestClient,  # noqa: F811 -- the fixture imported above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    resp = _post(client, "/api/v1/agents", _definition(sub, {"set": "read-everything"}))
    assert resp.status_code == 201, resp.text
    audit = (await _audit_rows())[-1].payload
    assert audit["op_id"] == "agent.create"
    assert audit["starting_grant_agent"] == sub
    assert audit["starting_grants"] == "read-everything"


@pytest.mark.asyncio
async def test_mcp_create_names_the_agent_that_gets_the_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("triage-bot")
    _switch(monkeypatch, "on")
    structlog.contextvars.clear_contextvars()
    await _create_handler(_operator(), _definition(sub, {"set": "read-everything"}))
    bound = structlog.contextvars.get_contextvars()
    assert bound["audit_op_id"] == "agent.create"
    assert bound["audit_starting_grant_agent"] == sub
    assert bound["audit_starting_grants"] == "read-everything"
