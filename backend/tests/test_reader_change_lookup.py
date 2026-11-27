# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Which calls of a Reader count as a change, and the answer of the call doors.

* A write at level ``safe`` (class ``write``, with or without the safe-write
  mark) is a change. A Reader's call of it is refused for every kind of
  login, through the real ``dispatch``; a plain read keeps its answer.
* Every built-in operation of class ``write`` or with the safe-write mark
  counts as a change.
* A credential read counts as a change; a credential read whose answer holds
  no secret value does not.
* The general call route and ``call_operation`` look the operation up
  before they answer a Reader: a change gets the code and the sentence, a
  plain read and an unknown operation get the plain answer of the door.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog
from sqlalchemy import select

from meho_backplane import op_classes
from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast.events import resolve_op_class
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.op_classes import (
    declare_no_secret_value,
    declare_op_classes,
    declare_safe_writes,
    is_safe_write,
)
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations.role_floors import READER_CHANGE_CODE, asks_for_change
from meho_backplane.operations.typed_register import run_typed_op_registrars
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service
from tests.test_agent_grant_required import _AGENT, _grant, _seed_vault_target
from tests.test_approved_run_rights import (
    _required_settings_env,
    _reset_state,
    stub_embedding_service,
)
from tests.test_reader_change_refused import (
    _ANSWER,
    _CONNECTOR,
    _KINDS,
    _PARAMS,
    _PLAIN_MCP,
    _READ_OP,
    _READERS,
    _REST_BODY,
    _RUNS,
    _assert_nothing_stored_or_run,
    _assert_reader_refused,
    _count,
    _mcp_call,
    _reader_tokens,
    client,  # noqa: F401 - pytest fixture
    kinds,  # noqa: F401 - pytest fixture
    mcp_as,  # noqa: F401 - pytest fixture
)

__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

_SAFE_WRITE_MARKED = "vault.reader.safe_write_marked"
_SAFE_WRITE = "vault.reader.safe_write"
_CREDENTIAL_READ = "vault.reader.secret_value_read"
_NO_SECRET_READ = "vault.reader.key_names_read"
_STAND_INS = (_SAFE_WRITE_MARKED, _SAFE_WRITE, _CREDENTIAL_READ, _NO_SECRET_READ)


async def run_safe_write_marked(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(_SAFE_WRITE_MARKED)
    return {"done": True}


async def run_safe_write(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(_SAFE_WRITE)
    return {"done": True}


async def run_credential_read(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(_CREDENTIAL_READ)
    return {"done": True}


async def run_no_secret_read(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(_NO_SECRET_READ)
    return {"keys": ["made-up-key"]}


#: op id -> the stand-in handler that counts its runs.
_HANDLERS: dict[str, Callable[..., Any]] = {
    _SAFE_WRITE_MARKED: run_safe_write_marked,
    _SAFE_WRITE: run_safe_write,
    _CREDENTIAL_READ: run_credential_read,
    _NO_SECRET_READ: run_no_secret_read,
}


@pytest.fixture
async def safe_kinds(
    kinds: None,  # noqa: F811 - the fixture above
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    """Two writes and two credential reads at level ``safe``."""
    del kinds
    declare_op_classes(
        write=[_SAFE_WRITE_MARKED, _SAFE_WRITE], credential_read=[_CREDENTIAL_READ, _NO_SECRET_READ]
    )
    declare_safe_writes([_SAFE_WRITE_MARKED])
    declare_no_secret_value([_NO_SECRET_READ])
    for op_id in _STAND_INS:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=_HANDLERS[op_id],
            summary="Stand-in op.",
            description="Counts its runs.",
            parameter_schema={"type": "object"},
            safety_level="safe",
            requires_approval=False,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    yield
    for op_id in _STAND_INS:
        op_classes._DECLARED.pop(op_id, None)
    op_classes._SAFE_WRITES.discard(_SAFE_WRITE_MARKED)
    op_classes._NO_SECRET_VALUE.discard(_NO_SECRET_READ)


async def _reader_dispatch(login: str, op_id: str, target: Any = None) -> Any:
    """Dispatch *op_id* as a kind of Reader login; ``hosted`` is a hosted agent run."""
    target = target or await _seed_vault_target()
    if login != "hosted":
        return await dispatch(
            operator=_READERS[login],
            connector_id=_CONNECTOR,
            op_id=op_id,
            target=target,
            params=_PARAMS,
        )
    await _grant(op_id, "auto-execute")
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: _AGENT}):
        return await dispatch(
            operator=_READERS["person_token"],
            connector_id=_CONNECTOR,
            op_id=op_id,
            target=target,
            params=_PARAMS,
        )


_LOGINS = ["person_token", "person_console", "service", "agent", "hosted"]


@pytest.mark.parametrize("login", _LOGINS)
@pytest.mark.parametrize("op_id", [_SAFE_WRITE_MARKED, _SAFE_WRITE])
@pytest.mark.usefixtures("safe_kinds")
async def test_a_reader_write_at_level_safe_is_refused(login: str, op_id: str) -> None:
    result = await _reader_dispatch(login, op_id)

    _assert_reader_refused(result)
    await _assert_nothing_stored_or_run()


@pytest.mark.parametrize("login", _LOGINS)
@pytest.mark.usefixtures("safe_kinds")
async def test_a_reader_plain_read_keeps_its_answer_next_to_a_safe_write(login: str) -> None:
    result = await _reader_dispatch(login, _READ_OP)

    assert result.extras.get("reason_code") != READER_CHANGE_CODE, result
    if login in {"person_token", "person_console", "service"}:
        assert result.status == "ok", result
        assert _RUNS == [_READ_OP]


async def test_every_built_in_write_counts_as_a_change(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read from the operation registry, not a hand list."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    try:
        await run_typed_op_registrars(embedding_service=fake_embedding_service())
    finally:
        get_settings.cache_clear()
    async with get_sessionmaker()() as session:
        rows = (
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.tenant_id.is_(None))
                )
            )
            .scalars()
            .all()
        )

    writes = [
        row
        for row in rows
        if resolve_op_class(row.op_id, row.op_class) == "write" or is_safe_write(row.op_id)
    ]
    assert len(writes) > 10
    missed = []
    for row in writes:
        # The level of the row does not matter: a write is a change also at ``safe``.
        row.safety_level, row.requires_approval = "safe", False
        if not asks_for_change(row, {}):
            missed.append(row.op_id)
    assert missed == []


@pytest.mark.usefixtures("safe_kinds")
async def test_a_reader_credential_read_is_a_change_unless_it_shows_no_secret_value() -> None:
    target = await _seed_vault_target()
    refused = await _reader_dispatch("person_token", _CREDENTIAL_READ, target)
    _assert_reader_refused(refused)
    await _assert_nothing_stored_or_run()

    ran = await _reader_dispatch("person_token", _NO_SECRET_READ, target)
    assert ran.status == "ok", ran
    assert _RUNS == [_NO_SECRET_READ]
    assert await _count(ApprovalRequest) == 0


# --- The general call doors look the operation up ----------------------------------------

_PLAIN_REST = {"detail": READER_CHANGE_CODE}
_UNKNOWN_OP = "vault.reader.no_such_operation"


def _call_body(op_id: str) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR,
        "op_id": op_id,
        "target": "vault-grant-test",
        "params": _PARAMS,
    }


@pytest.mark.parametrize("login", ["person", "service", "agent"])
@pytest.mark.usefixtures("safe_kinds")
async def test_the_rest_call_route_looks_the_operation_up_for_a_reader(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    login: str,
) -> None:
    ac, key = client
    headers = _reader_tokens(key)[login]
    await _seed_vault_target()
    expected = {
        _READ_OP: _PLAIN_REST,
        _UNKNOWN_OP: _PLAIN_REST,
        _KINDS["write_caution"][0]: _REST_BODY,
        _SAFE_WRITE: _REST_BODY,
        _CREDENTIAL_READ: _REST_BODY,
    }

    for op_id, body in expected.items():
        response = await ac.post("/api/v1/operations/call", json=_call_body(op_id), headers=headers)
        assert response.status_code == 403, (op_id, response.text)
        assert response.json() == body, op_id

    await _assert_nothing_stored_or_run()


@pytest.mark.parametrize("login", ["person_token", "service", "agent"])
@pytest.mark.usefixtures("safe_kinds")
async def test_call_operation_looks_the_operation_up_for_a_reader(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    mcp_as: Callable[[Operator], None],  # noqa: F811
    login: str,
) -> None:
    ac, _ = client
    mcp_as(_READERS[login])
    expected = {
        _READ_OP: _PLAIN_MCP,
        _UNKNOWN_OP: _PLAIN_MCP,
        _KINDS["write_caution"][0]: _ANSWER,
        _SAFE_WRITE: _ANSWER,
        _CREDENTIAL_READ: _ANSWER,
    }

    for op_id, message in expected.items():
        body = await _mcp_call(ac, "call_operation", _call_body(op_id))
        assert body["error"]["code"] == INVALID_PARAMS, body
        assert body["error"]["message"] == message, op_id

    await _assert_nothing_stored_or_run()


@pytest.mark.usefixtures("safe_kinds")
async def test_an_operator_call_through_the_route_is_not_looked_up_as_a_reader(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
) -> None:
    from tests.test_approved_run_rights import _token

    ac, key = client
    await _seed_vault_target()
    headers = _token(key, "operator-person", role=TenantRole.OPERATOR)

    response = await ac.post(
        "/api/v1/operations/call", json=_call_body(_SAFE_WRITE), headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "ok"
    assert _RUNS == [_SAFE_WRITE]
