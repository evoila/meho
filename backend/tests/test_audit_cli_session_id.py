"""The ``Meho-Session-Id`` header the CLI sends lands on the audit rows."""

from __future__ import annotations

import uuid

from meho_backplane.operations._audit import (
    agent_session_id_var,
    client_session_id_var,
    resolve_agent_session_id,
)

_SESSION = uuid.UUID("11111111-2222-4333-8444-555555555555")


def test_client_session_id_is_used_when_no_other_session_is_bound() -> None:
    token = client_session_id_var.set(_SESSION)
    try:
        assert resolve_agent_session_id() == _SESSION
    finally:
        client_session_id_var.reset(token)


def test_agent_run_wins_over_client_session_id() -> None:
    run_id = uuid.uuid4()
    run_token = agent_session_id_var.set(run_id)
    client_token = client_session_id_var.set(_SESSION)
    try:
        assert resolve_agent_session_id() == run_id
    finally:
        client_session_id_var.reset(client_token)
        agent_session_id_var.reset(run_token)


def test_no_session_bound_gives_none() -> None:
    assert resolve_agent_session_id() is None
