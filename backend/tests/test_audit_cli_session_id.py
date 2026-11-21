"""The ``Meho-Session-Id`` header the CLI sends lands on the audit rows."""

from __future__ import annotations

import uuid

import pytest

from meho_backplane.operations._audit import (
    agent_session_id_var,
    client_session_id_var,
    resolve_agent_session_id,
    resolve_audit_session_id,
)

_SESSION = uuid.UUID("11111111-2222-4333-8444-555555555555")


def test_client_session_id_is_used_when_no_other_session_is_bound() -> None:
    token = client_session_id_var.set(_SESSION)
    try:
        assert resolve_audit_session_id() == _SESSION
    finally:
        client_session_id_var.reset(token)


def test_agent_run_wins_over_client_session_id() -> None:
    run_id = uuid.uuid4()
    run_token = agent_session_id_var.set(run_id)
    client_token = client_session_id_var.set(_SESSION)
    try:
        assert resolve_audit_session_id() == run_id
        assert resolve_agent_session_id() == run_id
    finally:
        client_session_id_var.reset(client_token)
        agent_session_id_var.reset(run_token)


def test_no_session_bound_gives_none() -> None:
    assert resolve_audit_session_id() is None
    assert resolve_agent_session_id() is None


def test_client_session_id_is_not_an_agent_session() -> None:
    token = client_session_id_var.set(_SESSION)
    try:
        assert resolve_agent_session_id() is None
    finally:
        client_session_id_var.reset(token)


async def test_reflex_advisory_ignores_client_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
    from meho_backplane.broadcast import reflex

    monkeypatch.setattr(
        reflex, "get_settings", lambda: SimpleNamespace(reflex_advisory_window_minutes=5)
    )

    async def _must_not_claim(*_a: object, **_k: object) -> bool:
        raise AssertionError("a CLI session must not reach the advisory dedupe")

    monkeypatch.setattr(reflex, "_claim", _must_not_claim)
    operator = Operator(
        sub="human-sub",
        name="human-sub",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    token = client_session_id_var.set(_SESSION)
    try:
        assert (
            await reflex.build_reflex_advisory(operator, op_class="write", target_name=None) == {}
        )
    finally:
        client_session_id_var.reset(token)
