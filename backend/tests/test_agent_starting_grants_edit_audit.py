# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The audit row and the error text of an agent definition edit with a starting grant pick.

* The audit row of an edit that points the definition at another agent
  names that agent (``starting_grant_agent``), next to the picked set.
* A refused edit says "Nothing was changed.", a refused create
  "Nothing was created.".

The helpers and the autouse fixtures come from
``test_agent_starting_grants.py`` and ``test_agent_starting_grants_edit.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog
from fastapi.testclient import TestClient

from meho_backplane.main import app
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.agents import _create_handler, _edit_handler
from tests.test_agent_starting_grants import (  # noqa: F401 -- the autouse fixtures apply by import
    _TENANT_A,
    _audit_rows,
    _definition,
    _operator,
    _seed_grant,
    _seed_principal,
    _seed_tenants,
    _settings_env,
    _stub_vault_write,
    _switch,
)
from tests.test_agent_starting_grants_edit import (
    _identity_ref,
    _seed_definition,
    _send,
)

from ._vault_fakes import install_fake_vault

_UNKNOWN_TARGET = {"set": "read-targets", "targets": ["nope"]}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.mark.asyncio
async def test_patch_audit_row_names_the_agent_that_got_the_grants(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new = await _seed_definition()
    _switch(monkeypatch, "on")
    resp = _send(
        client,
        "PATCH",
        "/api/v1/agents/triage",
        {"identity_ref": new, "starting_grants": {"set": "read-everything"}},
    )
    assert resp.status_code == 200, resp.text
    audit = (await _audit_rows())[-1].payload
    assert audit["op_id"] == "agent.edit"
    assert audit["agent_name"] == "triage"
    assert audit["starting_grant_agent"] == new
    assert audit["starting_grants"] == "read-everything"


@pytest.mark.asyncio
async def test_patch_without_a_pick_names_the_agent_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old, new = await _seed_definition()
    await _seed_grant(new)
    _switch(monkeypatch, "on")
    resp = _send(client, "PATCH", "/api/v1/agents/triage", {"identity_ref": new})
    assert resp.status_code == 200, resp.text
    audit = (await _audit_rows())[-1].payload
    assert audit["starting_grant_agent"] == new
    assert audit["starting_grants"] == "not-chosen"


@pytest.mark.asyncio
async def test_patch_unknown_target_says_nothing_was_changed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, new = await _seed_definition()
    _switch(monkeypatch, "on")
    resp = _send(
        client,
        "PATCH",
        "/api/v1/agents/triage",
        {"identity_ref": new, "starting_grants": _UNKNOWN_TARGET},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail.endswith("Nothing was changed."), detail
    assert "created" not in detail
    assert await _identity_ref() == old


@pytest.mark.asyncio
async def test_mcp_edit_names_the_agent_and_says_nothing_was_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old, new = await _seed_definition()
    _switch(monkeypatch, "on")
    structlog.contextvars.clear_contextvars()
    with pytest.raises(McpInvalidParamsError, match=r"Nothing was changed\.$"):
        await _edit_handler(
            _operator(), {"name": "triage", "identity_ref": new, "starting_grants": _UNKNOWN_TARGET}
        )
    bound = structlog.contextvars.get_contextvars()
    assert bound["audit_op_id"] == "agent.edit"
    assert bound["audit_starting_grant_agent"] == new
    assert await _identity_ref() == old


@pytest.mark.asyncio
async def test_mcp_edit_with_bad_arguments_still_names_the_operation() -> None:
    await _seed_definition()
    structlog.contextvars.clear_contextvars()
    with pytest.raises(McpInvalidParamsError, match="invalid arguments"):
        await _edit_handler(_operator(), {"name": "triage", "turn_budget": 0})
    bound = structlog.contextvars.get_contextvars()
    assert bound["audit_op_id"] == "agent.edit"
    assert bound["audit_agent_name"] == "triage"


@pytest.mark.asyncio
async def test_create_unknown_target_says_nothing_was_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _seed_tenants()
    sub = await _seed_principal("create-bot")
    _switch(monkeypatch, "on")
    with pytest.raises(McpInvalidParamsError, match=r"Nothing was created\.$"):
        await _create_handler(_operator(), _definition(sub, _UNKNOWN_TARGET))
