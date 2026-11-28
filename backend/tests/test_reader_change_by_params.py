# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A call that is a change because of its params, at the REST and MCP doors.

A Reader who asks for a change gets the Reader sentence only when MEHO can
read that it is a change; a body it cannot read gets the plain refusal of
the door. Two stand-in operations, all values made up:

* a ``safe`` read with no approval flag whose connector marks a call with
  the key ``secret`` in its params as a credential call
  (``declare_credential_check``): a change only by its params;
* a write at ``caution`` with no approval flag: a change whatever its
  params.

The REST call route and MCP ``call_operation`` count a call as a change
with the same checks as the policy gate. Params that are not a JSON object
cannot be read, so only what does not depend on the params decides. The
real ``dispatch`` refuses the same calls with ``insufficient_role``. An
Operator keeps its answers. The console call form is in
``test_reader_change_by_params_console.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import structlog

from meho_backplane import op_classes
from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.op_classes import declare_credential_check
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations.role_floors import READER_CHANGE_CODE
from tests.test_agent_grant_required import _AGENT, _grant, _seed_vault_target
from tests.test_approved_run_rights import (
    _required_settings_env,
    _reset_state,
    _token,
    stub_embedding_service,
)
from tests.test_reader_change_refused import (
    _ANSWER,
    _CONNECTOR,
    _PLAIN_MCP,
    _READERS,
    _REST_BODY,
    _RUNS,
    _assert_nothing_stored_or_run,
    _assert_reader_refused,
    _count,
    _mcp_call,
    _reader_tokens,
    client,  # noqa: F401 - pytest fixture
    mcp_as,  # noqa: F401 - pytest fixture
)

__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

#: A ``safe`` read with no approval flag; a call with ``secret`` in its
#: params is a credential call.
PARAMS_READ = "vault.params.read"
#: A write at ``caution`` with no approval flag.
PARAMS_WRITE = "vault.params.write"

_SECRET: dict[str, Any] = {"path": "x", "secret": "made-up"}
_PLAIN_REST = {"detail": READER_CHANGE_CODE}


def secret_in_params(params: Mapping[str, Any]) -> bool:
    """The stand-in connector's check: a call with ``secret`` is a credential call."""
    return "secret" in params


async def run_params_read(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(PARAMS_READ)
    return {"done": True}


async def run_params_write(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append(PARAMS_WRITE)
    return {"done": True}


@pytest.fixture
async def stand_ins(stub_embedding_service: AsyncMock) -> AsyncIterator[Any]:
    """The two stand-in operations; yields the target they run on."""
    from meho_backplane.connectors.registry import register_connector_v2
    from tests.test_operations_dispatcher import _NoOpVaultConnector

    _RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    declare_credential_check(PARAMS_READ, secret_in_params)
    for op_id, level, handler in (
        (PARAMS_READ, "safe", run_params_read),
        (PARAMS_WRITE, "caution", run_params_write),
    ):
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=handler,
            summary="Stand-in op.",
            description="Counts its runs.",
            parameter_schema={"type": "object"},
            safety_level=level,  # type: ignore[arg-type]
            requires_approval=False,
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    yield await _seed_vault_target()
    op_classes._CREDENTIAL_CHECKS.pop(PARAMS_READ, None)
    _RUNS.clear()


def _body(op_id: str, params: object) -> dict[str, Any]:
    return {
        "connector_id": _CONNECTOR,
        "op_id": op_id,
        "target": "vault-grant-test",
        "params": params,
    }


#: (op_id, params) -> True when a Reader gets the code and the sentence,
#: False when the Reader gets the plain answer of the door.
_READER_CASES: dict[str, tuple[str, object, bool]] = {
    "read with secret": (PARAMS_READ, _SECRET, True),
    "read with no params": (PARAMS_READ, {}, False),
    "read with a path": (PARAMS_READ, {"path": "x"}, False),
    "read with a list": (PARAMS_READ, ["secret"], False),
    "read with a string": (PARAMS_READ, "secret", False),
    "read with a number": (PARAMS_READ, 7, False),
    "write with a list": (PARAMS_WRITE, ["secret"], True),
    "write with a string": (PARAMS_WRITE, "secret", True),
    "write with a number": (PARAMS_WRITE, 7, True),
}


@pytest.mark.parametrize("case", list(_READER_CASES))
@pytest.mark.parametrize("login", ["person", "service", "agent"])
@pytest.mark.usefixtures("stand_ins")
async def test_a_reader_rest_call_is_a_change_by_its_params_as_at_the_gate(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    login: str,
    case: str,
) -> None:
    ac, key = client
    op_id, params, change = _READER_CASES[case]

    response = await ac.post(
        "/api/v1/operations/call", json=_body(op_id, params), headers=_reader_tokens(key)[login]
    )

    assert response.status_code == 403, response.text
    assert response.json() == (_REST_BODY if change else _PLAIN_REST)
    await _assert_nothing_stored_or_run()


_MCP_LOGINS = {"person": "person_token", "service": "service", "agent": "agent"}


@pytest.mark.parametrize("case", list(_READER_CASES))
@pytest.mark.parametrize("login", list(_MCP_LOGINS))
@pytest.mark.usefixtures("stand_ins")
async def test_a_reader_mcp_call_is_a_change_by_its_params_as_at_the_gate(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    mcp_as: Callable[[Operator], None],  # noqa: F811
    login: str,
    case: str,
) -> None:
    ac, _ = client
    mcp_as(_READERS[_MCP_LOGINS[login]])
    op_id, params, change = _READER_CASES[case]

    body = await _mcp_call(ac, "call_operation", _body(op_id, params))

    assert body["error"]["code"] == INVALID_PARAMS, body
    assert body["error"]["message"] == (_ANSWER if change else _PLAIN_MCP)
    await _assert_nothing_stored_or_run()


@pytest.mark.parametrize("login", ["person_token", "person_console", "service", "agent", "hosted"])
async def test_the_gate_refuses_a_reader_read_with_a_credential_by_its_params(
    stand_ins: Any, login: str
) -> None:
    """Where a door gives the sentence, the real ``dispatch`` refuses the call."""
    target = stand_ins
    operator = _READERS["person_token" if login == "hosted" else login]
    if login == "hosted":
        await _grant(PARAMS_READ, "auto-execute")
    with structlog.contextvars.bound_contextvars(
        **({ACTOR_SUB_KEY: _AGENT} if login == "hosted" else {})
    ):
        result = await dispatch(
            operator=operator,
            connector_id=_CONNECTOR,
            op_id=PARAMS_READ,
            target=target,
            params=dict(_SECRET),
        )

    _assert_reader_refused(result)
    await _assert_nothing_stored_or_run()


#: (op_id, params) -> (HTTP status, ``status`` of the answer or the first
#: error type, stored approval requests, ran). An Operator's answers.
_OPERATOR_CASES: dict[str, tuple[str, object, tuple[int, str, int, bool]]] = {
    "write with no params": (PARAMS_WRITE, {}, (200, "awaiting_approval", 1, False)),
    "write with secret": (PARAMS_WRITE, _SECRET, (200, "awaiting_approval", 1, False)),
    "read with secret": (PARAMS_READ, _SECRET, (200, "awaiting_approval", 1, False)),
    "read with no params": (PARAMS_READ, {}, (200, "ok", 0, True)),
    "read with a list": (PARAMS_READ, ["secret"], (422, "dict_type", 0, False)),
    "write with a string": (PARAMS_WRITE, "secret", (422, "dict_type", 0, False)),
    "write with a number": (PARAMS_WRITE, 7, (422, "dict_type", 0, False)),
}


@pytest.mark.parametrize("case", list(_OPERATOR_CASES))
@pytest.mark.usefixtures("stand_ins")
async def test_an_operator_rest_call_with_the_same_params_keeps_its_answer(
    client: tuple[httpx.AsyncClient, Any],  # noqa: F811
    case: str,
) -> None:
    ac, key = client
    op_id, params, expected = _OPERATOR_CASES[case]

    response = await ac.post(
        "/api/v1/operations/call",
        json=_body(op_id, params),
        headers=_token(key, "operator-person", role=TenantRole.OPERATOR),
    )

    body = response.json()
    answer = body["status"] if response.status_code == 200 else body["detail"][0]["type"]
    stored = await _count(ApprovalRequest)
    assert (response.status_code, answer, stored, bool(_RUNS)) == expected, response.text
