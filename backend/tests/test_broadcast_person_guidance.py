# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""People read the broadcast feed; only agents post (#5770).

MEHO refuses an announcement from a person's login. These tests pin that
every text a person's session gets agrees with that rule, and that the
refusal comes first:

* the session text (``initialize.instructions``) of a person's session has
  the "read the feed" step and no "announce" step; an agent principal's
  session still gets the full discipline;
* the Claude Code plugin skill and its hook tell a person to skip
  ``meho_broadcast_announce``;
* the tool description says first that only agents can post;
* a person's call with bad arguments gets ``agent_identity_required`` with
  the "skip this step" text, not an input error; an agent's call with bad
  arguments still gets the input error.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.auth.agent_admission import (
    AGENT_IDENTITY_REQUIRED,
    NOT_AN_AGENT_REMEDIATION,
    is_agent_caller,
)
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.conventions.preamble import (
    BROADCAST_DISCIPLINE_BAND,
    BROADCAST_READER_BAND,
    assemble_preamble,
)
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS, PROTOCOL_VERSION
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.broadcast import _handler_announce
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 -- pytest-discovered fixture
)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PLUGIN = _REPO_ROOT / "clients" / "claude-code-plugin"

#: Phrases that tell a session to post an announcement.
_ANNOUNCE_STEPS = ('phase="start"', 'phase="update"', 'phase="completion"', "Announce intent")

_BAD_ARGUMENTS: list[dict[str, Any]] = [
    {},  # 'activity' is missing
    {"activity": ""},  # too short
    {"activity": "x", "unknown_field": 1},  # extra field
    {"activity": "x", "phase": "later"},  # not one of the phases
]


def _person() -> Operator:
    return build_operator(TenantRole.OPERATOR)


def _agent() -> Operator:
    return _person().model_copy(
        update={"principal_kind": PrincipalKind.AGENT, "client_id": "agent:triage"}
    )


@contextmanager
def _client_as(operator: Operator) -> Iterator[TestClient]:
    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


def _instructions(client: TestClient) -> str:
    body = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-harness", "version": "0.0.0"},
            },
        },
    ).json()
    assert "error" not in body
    return str(body["result"]["instructions"])


def _announce_on_the_wire(client: TestClient, arguments: dict[str, Any]) -> dict[str, Any]:
    body = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "meho_broadcast_announce", "arguments": arguments},
        },
    ).json()
    error: dict[str, Any] = body["error"]
    return error


# ---------------------------------------------------------------------------
# Who may post
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [PrincipalKind.USER, PrincipalKind.SERVICE])
def test_a_login_that_is_not_an_agent_cannot_post(kind: PrincipalKind) -> None:
    operator = _person().model_copy(update={"principal_kind": kind})
    assert is_agent_caller(operator) is False


def test_an_agent_token_and_a_hosted_run_may_post() -> None:
    assert is_agent_caller(_agent()) is True
    token = current_agent_run_id_var.set(uuid.uuid4())
    try:
        # Inside a hosted run, the run's login may post whatever it is.
        assert is_agent_caller(_person()) is True
    finally:
        current_agent_run_id_var.reset(token)


# ---------------------------------------------------------------------------
# Session text
# ---------------------------------------------------------------------------


async def test_reader_band_keeps_read_step_and_drops_announce_steps() -> None:
    result = await assemble_preamble(uuid.uuid4(), "person-1", broadcast_can_post=False)
    assert result.text == BROADCAST_READER_BAND
    assert "meho_broadcast_recent" in result.text
    assert "cannot post" in result.text
    for step in _ANNOUNCE_STEPS:
        assert step not in result.text


async def test_reader_band_is_left_out_when_the_session_has_no_broadcast_tools() -> None:
    result = await assemble_preamble(
        uuid.uuid4(), "person-1", include_broadcast=False, broadcast_can_post=False
    )
    assert result.text == ""


@pytest.mark.usefixtures("seeded_operator_tenant")
def test_a_person_session_gets_no_announce_instruction() -> None:
    with _client_as(_person()) as client:
        instructions = _instructions(client)
    assert BROADCAST_READER_BAND in instructions
    assert BROADCAST_DISCIPLINE_BAND not in instructions
    for step in _ANNOUNCE_STEPS:
        assert step not in instructions


@pytest.mark.usefixtures("seeded_operator_tenant")
def test_an_agent_principal_session_gets_the_full_discipline() -> None:
    with _client_as(_agent()) as client:
        instructions = _instructions(client)
    assert instructions.startswith(BROADCAST_DISCIPLINE_BAND)
    assert BROADCAST_READER_BAND not in instructions
    assert "Announce intent" in instructions
    # #4113: the band tells the agent to skip a step whose tool its client
    # does not offer (MEHO cannot see client-side tool hiding).
    assert "If your client does not offer one of these tools, skip that step." in instructions


# ---------------------------------------------------------------------------
# Plugin text
# ---------------------------------------------------------------------------


def test_plugin_skill_tells_a_person_to_read_and_not_to_post() -> None:
    skill = (_PLUGIN / "skills" / "broadcast" / "SKILL.md").read_text(encoding="utf-8")
    assert "People read the feed; only agents post." in skill
    assert "meho_broadcast_recent" in skill
    assert "A person's login cannot post." in skill
    for step in _ANNOUNCE_STEPS:
        assert step not in skill


def test_plugin_hooks_never_ask_for_an_announcement() -> None:
    hooks = json.loads((_PLUGIN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    # The Stop reminder ("report with phase=completion") and the marker hook
    # it needed are gone: a person's login cannot post.
    assert set(hooks["hooks"]) == {"SessionStart", "PreToolUse"}
    assert not (_PLUGIN / "bin" / "stop-report.sh").exists()
    assert not (_PLUGIN / "bin" / "post-announce.sh").exists()


def test_plugin_call_operation_reminder_says_to_skip_announce(tmp_path: pathlib.Path) -> None:
    script = _PLUGIN / "bin" / "pre-call-operation.sh"

    def _run() -> str:
        # The hook keeps its once-per-session marker under TMPDIR; point the
        # child process at this test's own folder.
        return subprocess.run(
            ["sh", str(script)],
            input='{"session_id":"s-5770"}',
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": "/usr/bin:/bin", "TMPDIR": str(tmp_path)},
        ).stdout

    first = json.loads(_run())
    message = first["hookSpecificOutput"]["additionalContext"]
    assert "meho_broadcast_recent" in message
    assert "skip meho_broadcast_announce" in message
    assert "phase=start" not in message
    assert "phase=completion" not in message
    # Once per session.
    assert _run() == ""


# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------


def test_announce_description_says_first_that_people_cannot_post() -> None:
    entry = get_tool("meho_broadcast_announce")
    assert entry is not None
    # Drop the maturity label (for example "[beta] ") the registry adds.
    description = re.sub(r"^\[[a-z]+\] ", "", entry[0].description)
    first_paragraph = description.split("\n\n", 1)[0]
    assert first_paragraph.startswith("Agents only:")
    assert "A person's login cannot post here" in first_paragraph


# ---------------------------------------------------------------------------
# Refusal comes before the input check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("arguments", _BAD_ARGUMENTS)
def test_a_person_with_bad_arguments_gets_the_refusal_first(arguments: dict[str, Any]) -> None:
    with _client_as(_person()) as client:
        error = _announce_on_the_wire(client, arguments)
    assert error["code"] == INVALID_PARAMS
    assert error["data"]["code"] == AGENT_IDENTITY_REQUIRED
    assert error["data"]["remediation"] == NOT_AN_AGENT_REMEDIATION
    assert "Skip this step" in error["data"]["remediation"]
    assert "inputSchema" not in error["message"]


@pytest.mark.parametrize("arguments", _BAD_ARGUMENTS)
async def test_handler_checks_the_caller_before_the_arguments(arguments: dict[str, Any]) -> None:
    with pytest.raises(McpInvalidParamsError) as excinfo:
        await _handler_announce(_person(), arguments)
    assert excinfo.value.data is not None
    assert excinfo.value.data["code"] == AGENT_IDENTITY_REQUIRED
    assert excinfo.value.data["remediation"] == NOT_AN_AGENT_REMEDIATION


def test_an_agent_with_bad_arguments_still_gets_the_input_error() -> None:
    with _client_as(_agent()) as client:
        error = _announce_on_the_wire(client, {"activity": ""})
    assert error["code"] == INVALID_PARAMS
    assert "inputSchema" in error["message"]
    assert (error.get("data") or {}).get("code") != AGENT_IDENTITY_REQUIRED
