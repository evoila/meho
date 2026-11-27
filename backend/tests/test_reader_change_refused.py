# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A caller with the Reader role who asks for a change is refused at once.

The answer has the code ``insufficient_role`` and one plain sentence. MEHO
stores no approval request, and nothing runs. The tests go through the
whole path: the real ``dispatch``, the REST and MCP routes of the app, the
agent's own wait after an approval, and a hosted agent run. The operations
are stand-ins that count their runs, one per kind of change:

* a ``caution`` write and a ``dangerous`` write;
* a delete (``destructive``);
* a change of rights and a credential write, both at level ``safe``, so
  the rule does not lean on the risk level;
* a multi-step operation.

A walk over every REST route that does not only read, and over every MCP
tool, checks that a Reader stores nothing and runs nothing on any of them.
The console doors are in ``test_reader_change_refused_console.py``.
"""

from __future__ import annotations

import inspect
import re
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
import structlog
from fastapi.routing import APIRoute
from httpx import ASGITransport
from sqlalchemy import func, select

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.rbac import require_operator_for_call
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool, registered_tool_names
from meho_backplane.op_classes import declare_op_classes
from meho_backplane.operations import dispatch, register_typed_operation
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.grant_approval import register_rights_change_op
from meho_backplane.operations.role_floors import (
    READER_CHANGE_CODE,
    READER_CHANGE_SENTENCE,
    asks_for_change,
    reader_change_denial,
)
from tests import _composite_wait_audit_helpers as h
from tests._people_behind_helpers import give_known_person
from tests._route_tree_helpers import iter_routes
from tests.test_agent_grant_required import _AGENT, _TENANT, _grant, _operator, _seed_vault_target
from tests.test_approved_run_rights import (
    _approve,
    _person,
    _required_settings_env,
    _reset_state,
    _token,
    stub_embedding_service,
)
from tests.test_operations_dispatcher import _NoOpVaultConnector

__all__ = ["_required_settings_env", "_reset_state", "stub_embedding_service"]

_CONNECTOR = "vault-1.x"
_PARAMS: dict[str, Any] = {"path": "app/config"}

#: kind -> (op_id, risk level). The multi-step operation is a composite.
_KINDS: dict[str, tuple[str, str]] = {
    "write_caution": ("vault.reader.write_caution", "caution"),
    "write_dangerous": ("vault.reader.write_dangerous", "dangerous"),
    "delete": ("vault.reader.delete", "destructive"),
    "rights_change": ("vault.reader.rights_change", "safe"),
    "credential_write": ("vault.reader.credential_write", "safe"),
    "multi_step": ("vault.reader.multi_step", "caution"),
}
_READ_OP = "vault.reader.plain_read"

#: Every run of a stand-in operation, by op id.
_RUNS: list[str] = []

_ANSWER = f"{READER_CHANGE_CODE}: {READER_CHANGE_SENTENCE}"
_PLAIN_MCP = "forbidden: 'call_operation' requires a higher role"
_REST_BODY = {"detail": READER_CHANGE_CODE, "message": READER_CHANGE_SENTENCE}


async def run_write_caution(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.write_caution")
    return {"done": True}


async def run_write_dangerous(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.write_dangerous")
    return {"done": True}


async def run_delete(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.delete")
    return {"done": True}


async def run_rights_change(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.rights_change")
    return {"done": True}


async def run_credential_write(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.credential_write")
    return {"done": True}


async def run_plain_read(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    del operator, target, params
    _RUNS.append("vault.reader.plain_read")
    return {"done": True}


#: op id -> the stand-in handler that counts its runs.
_HANDLERS: dict[str, Callable[..., Any]] = {
    "vault.reader.write_caution": run_write_caution,
    "vault.reader.write_dangerous": run_write_dangerous,
    "vault.reader.delete": run_delete,
    "vault.reader.rights_change": run_rights_change,
    "vault.reader.credential_write": run_credential_write,
    "vault.reader.plain_read": run_plain_read,
}


async def multi_step_composite(operator: Operator, target: Any, params: dict[str, Any]) -> Any:
    """The stand-in multi-step operation; it counts its run."""
    del operator, target, params
    _RUNS.append(_KINDS["multi_step"][0])
    return {"done": True}


@pytest.fixture
async def kinds(stub_embedding_service: AsyncMock) -> AsyncIterator[None]:
    """One stand-in operation per kind of change, and one plain read."""
    _RUNS.clear()
    register_connector_v2(product="vault", version="", impl_id="", cls=_NoOpVaultConnector)
    declare_op_classes(credential_write=[_KINDS["credential_write"][0]])
    register_rights_change_op(_KINDS["rights_change"][0])
    typed = [(op_id, level) for kind, (op_id, level) in _KINDS.items() if kind != "multi_step"]
    for op_id, level in [*typed, (_READ_OP, "safe")]:
        await register_typed_operation(
            product="vault",
            version="1.x",
            impl_id="vault",
            op_id=op_id,
            handler=_HANDLERS[op_id],
            summary="Stand-in op.",
            description="Counts its runs.",
            parameter_schema={"type": "object"},
            safety_level=level,  # type: ignore[arg-type]
            requires_approval=level == "destructive",
            when_to_use=None,
            embedding_service=stub_embedding_service,
        )
    async with get_sessionmaker()() as session:
        await h.register_composite(
            session,
            _KINDS["multi_step"][0],
            "multi_step_composite",
            safety_level="caution",
            module=__name__,
        )
    yield
    _RUNS.clear()


def _reader(kind: PrincipalKind, *, web_session: bool = False) -> Operator:
    agent = kind is PrincipalKind.AGENT
    return Operator(
        sub=f"reader-{kind.value}",
        name="Reader",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.READ_ONLY,
        principal_kind=kind,
        client_id=_AGENT if agent else ("svc-reader" if kind is PrincipalKind.SERVICE else None),
        web_session=web_session,
    )


#: Every kind of login with the Reader role: a person's token, a person in
#: the web console, a service login and an agent login.
_READERS: dict[str, Operator] = {
    "person_token": _reader(PrincipalKind.USER),
    "person_console": _reader(PrincipalKind.USER, web_session=True),
    "service": _reader(PrincipalKind.SERVICE),
    "agent": _reader(PrincipalKind.AGENT),
}


async def _count(model: type[Any]) -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


async def _descriptor(op_id: str) -> EndpointDescriptor:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == op_id)
            )
        ).scalar_one()
        session.expunge(row)
        return row


async def _assert_nothing_stored_or_run() -> None:
    assert _RUNS == []
    assert await _count(ApprovalRequest) == 0


def _assert_reader_refused(result: Any) -> None:
    assert result.status == "denied", result
    assert result.extras["error_code"] == READER_CHANGE_CODE
    assert result.extras["reason_code"] == READER_CHANGE_CODE
    assert result.extras["reason"] == _ANSWER


# --- The rule ----------------------------------------------------------------------


def test_the_reader_sentence_is_the_exact_text() -> None:
    assert READER_CHANGE_SENTENCE == (
        "You have the Reader role. A Reader cannot ask for changes, so MEHO did not "
        "store or run this call. Ask an admin for a role that may make this change."
    )


@pytest.mark.usefixtures("kinds")
async def test_every_kind_asks_for_a_change_and_a_plain_read_does_not() -> None:
    for op_id, _level in _KINDS.values():
        assert asks_for_change(await _descriptor(op_id), _PARAMS), op_id
    assert not asks_for_change(await _descriptor(_READ_OP), _PARAMS)


@pytest.mark.usefixtures("kinds")
async def test_the_rule_counts_only_the_reader_role() -> None:
    descriptor = await _descriptor(_KINDS["write_caution"][0])
    for role in (TenantRole.OPERATOR, TenantRole.TENANT_ADMIN):
        caller = _person("person-1", role)
        assert reader_change_denial(caller, descriptor, _PARAMS) is None
    reader = _READERS["person_token"]
    assert reader_change_denial(reader, descriptor, _PARAMS) == _ANSWER
    assert reader_change_denial(reader, await _descriptor(_READ_OP), _PARAMS) is None


# --- A new call through the real dispatch --------------------------------------------


@pytest.mark.parametrize("login", list(_READERS))
@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.usefixtures("kinds")
async def test_a_reader_call_of_each_kind_is_refused(login: str, kind: str) -> None:
    reader = _READERS[login]
    target = await _seed_vault_target()

    result = await dispatch(
        operator=reader,
        connector_id=_CONNECTOR,
        op_id=_KINDS[kind][0],
        target=target,
        params=_PARAMS,
    )

    _assert_reader_refused(result)
    await _assert_nothing_stored_or_run()
    async with get_sessionmaker()() as session:
        (row,) = (
            (await session.execute(select(AuditLog).where(AuditLog.path == _KINDS[kind][0])))
            .scalars()
            .all()
        )
    assert row.payload["result_status"] == "denied"
    assert row.payload["error"]["reason_code"] == READER_CHANGE_CODE


@pytest.mark.parametrize("login", ["person_token", "person_console", "service"])
@pytest.mark.usefixtures("kinds")
async def test_a_reader_plain_read_runs_at_once(login: str) -> None:
    target = await _seed_vault_target()

    result = await dispatch(
        operator=_READERS[login],
        connector_id=_CONNECTOR,
        op_id=_READ_OP,
        target=target,
        params=_PARAMS,
    )

    assert result.status == "ok", result
    assert _RUNS == [_READ_OP]


@pytest.mark.usefixtures("kinds")
async def test_a_reader_agent_plain_read_is_not_refused_by_the_reader_rule() -> None:
    target = await _seed_vault_target()

    result = await dispatch(
        operator=_READERS["agent"],
        connector_id=_CONNECTOR,
        op_id=_READ_OP,
        target=target,
        params=_PARAMS,
    )

    assert result.extras.get("reason_code") != READER_CHANGE_CODE, result


@pytest.mark.parametrize("kind", list(_KINDS))
@pytest.mark.usefixtures("kinds")
async def test_a_hosted_agent_run_started_by_a_reader_is_refused(kind: str) -> None:
    await _grant(_KINDS[kind][0], "auto-execute")
    target = await _seed_vault_target()

    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: _AGENT}):
        result = await dispatch(
            operator=_READERS["person_token"],
            connector_id=_CONNECTOR,
            op_id=_KINDS[kind][0],
            target=target,
            params=_PARAMS,
        )

    _assert_reader_refused(result)
    await _assert_nothing_stored_or_run()


@pytest.mark.usefixtures("kinds")
async def test_the_gate_answer_is_the_same_for_a_decision_only_check() -> None:
    """A preview verdict and ``meho policy explain`` read the same answer."""
    descriptor = await _descriptor(_KINDS["write_caution"][0])

    verdict, reason = await policy_gate(
        operator=_READERS["person_token"],
        descriptor=descriptor,
        target=None,
        connector_id=_CONNECTOR,
        params=_PARAMS,
        decide_only=True,
    )

    assert verdict.value == "deny"
    assert reason == _ANSWER


# --- What does not change -----------------------------------------------------------


def _operator_login(kind: str, role: TenantRole) -> Operator:
    if kind == "service":
        return Operator(
            sub="service-1",
            name="Service",
            email=None,
            raw_jwt="<test-jwt>",
            tenant_id=_TENANT,
            tenant_role=role,
            principal_kind=PrincipalKind.SERVICE,
            client_id="svc-client",
        )
    if kind == "agent":
        return _operator()
    return _person("person-1", role)


@pytest.mark.parametrize(
    ("login", "role"),
    [
        ("person", TenantRole.OPERATOR),
        ("person", TenantRole.TENANT_ADMIN),
        ("service", TenantRole.OPERATOR),
        ("agent", TenantRole.OPERATOR),
    ],
)
@pytest.mark.parametrize("kind", ["write_caution", "write_dangerous", "multi_step"])
@pytest.mark.usefixtures("kinds")
async def test_an_operator_admin_agent_or_service_change_waits_for_approval(
    login: str, role: TenantRole, kind: str
) -> None:
    caller = _operator_login(login, role)
    if login == "agent":
        await _grant(_KINDS[kind][0], "needs-approval")
    target = await _seed_vault_target()
    await give_known_person(caller)

    result = await dispatch(
        operator=caller,
        connector_id=_CONNECTOR,
        op_id=_KINDS[kind][0],
        target=target,
        params=_PARAMS,
    )

    assert result.status == "awaiting_approval", result
    assert _RUNS == []
    assert await _count(ApprovalRequest) == 1


@pytest.mark.usefixtures("kinds")
async def test_an_operator_delete_without_a_preview_is_refused_for_a_missing_preview() -> None:
    target = await _seed_vault_target()
    caller = _person("person-1", TenantRole.OPERATOR)
    await give_known_person(caller)

    result = await dispatch(
        operator=caller,
        connector_id=_CONNECTOR,
        op_id=_KINDS["delete"][0],
        target=target,
        params=_PARAMS,
    )

    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    await _assert_nothing_stored_or_run()


@pytest.mark.usefixtures("kinds")
async def test_an_operator_caution_write_in_the_console_runs_at_once() -> None:
    target = await _seed_vault_target()
    caller = _person("person-1", TenantRole.OPERATOR).model_copy(update={"web_session": True})

    result = await dispatch(
        operator=caller,
        connector_id=_CONNECTOR,
        op_id=_KINDS["write_caution"][0],
        target=target,
        params=_PARAMS,
    )

    assert result.status == "ok", result
    assert [_KINDS["write_caution"][0]] == _RUNS


# --- An approved request whose requester is now a Reader ------------------------------


@pytest.mark.usefixtures("kinds")
async def test_an_agent_whose_role_is_now_reader_does_not_run_its_approved_call() -> None:
    """The resume of an approved call is handed a login with the Reader role.

    The approved-run branch of ``dispatch`` refuses that login with the code
    and the sentence, and the operation does not run.
    """
    from meho_backplane.agent.approval_wait import _resume_approved_or_already_resumed

    op_id = _KINDS["write_caution"][0]
    await _grant(op_id, "needs-approval")
    agent = _operator()
    target = await _seed_vault_target()
    parked = await dispatch(
        operator=agent, connector_id=_CONNECTOR, op_id=op_id, target=target, params=_PARAMS
    )
    assert parked.status == "awaiting_approval", parked
    request_id = uuid.UUID(parked.extras["approval_request_id"])
    await _approve(request_id, _person("reviewer-1"))

    envelope = await _resume_approved_or_already_resumed(
        operator=agent.model_copy(update={"tenant_role": TenantRole.READ_ONLY}),
        call_arguments={
            "connector_id": _CONNECTOR,
            "op_id": op_id,
            "params": _PARAMS,
            "target": "vault-grant-test",
        },
        awaiting_envelope={"op_id": op_id, "extras": {}},
        approval_request_id=request_id,
    )

    assert envelope["status"] == "denied", envelope
    assert envelope["extras"]["reason_code"] == READER_CHANGE_CODE
    assert READER_CHANGE_SENTENCE in envelope["extras"]["reason"]
    assert _RUNS == []


# --- The REST and MCP doors ---------------------------------------------------------


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    from tests._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks

    key = make_rsa_keypair("kid-reader-change")
    with respx.mock(assert_all_mocked=False, assert_all_called=False) as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="https://testserver",
        ) as ac:
            yield ac, key


def _reader_tokens(key: Any) -> dict[str, dict[str, str]]:
    """A Reader person token, a Reader service login and a Reader agent login."""
    reader = TenantRole.READ_ONLY
    return {
        "person": _token(key, "reader-person", role=reader),
        "service": _token(key, "reader-service", role=reader, kind="service", client_id="svc-r"),
        "agent": _token(key, _operator().sub, role=reader, kind="agent", client_id=_AGENT),
    }


@pytest.mark.parametrize("login", ["person", "service", "agent"])
@pytest.mark.usefixtures("kinds")
async def test_a_reader_rest_call_of_each_kind_gets_the_code_and_the_sentence(
    client: tuple[httpx.AsyncClient, Any], login: str
) -> None:
    ac, key = client
    headers = _reader_tokens(key)[login]
    await _seed_vault_target()

    for op_id, _level in _KINDS.values():
        response = await ac.post(
            "/api/v1/operations/call",
            json={
                "connector_id": _CONNECTOR,
                "op_id": op_id,
                "target": "vault-grant-test",
                "params": _PARAMS,
            },
            headers=headers,
        )
        assert response.status_code == 403, (op_id, response.text)
        assert response.json() == _REST_BODY

    await _assert_nothing_stored_or_run()


@pytest.mark.usefixtures("kinds")
async def test_a_reader_rest_preview_keeps_its_answer(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client

    response = await ac.post(
        "/api/v1/operations/preview",
        json={"connector_id": _CONNECTOR, "op_id": _KINDS["write_caution"][0], "params": _PARAMS},
        headers=_reader_tokens(key)["person"],
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": "insufficient_role"}


@pytest.fixture
def mcp_as() -> Iterator[Callable[[Operator], None]]:
    """Bind the MCP login of the next calls; the override is removed at the end."""

    def _bind(operator: Operator) -> None:
        async def _verify() -> Operator:
            return operator

        app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify

    yield _bind
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _mcp_call(ac: httpx.AsyncClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


@pytest.mark.parametrize("login", ["person_token", "service", "agent"])
@pytest.mark.usefixtures("kinds")
async def test_a_reader_mcp_call_of_each_kind_gets_the_code_and_the_sentence(
    client: tuple[httpx.AsyncClient, Any], mcp_as: Callable[[Operator], None], login: str
) -> None:
    ac, _ = client
    mcp_as(_READERS[login])

    for op_id, _level in _KINDS.values():
        body = await _mcp_call(
            ac,
            "call_operation",
            {
                "connector_id": _CONNECTOR,
                "op_id": op_id,
                "target": "vault-grant-test",
                "params": {},
            },
        )
        assert body["error"]["message"] == _ANSWER, body

    preview = await _mcp_call(
        ac, "preview_operation", {"connector_id": _CONNECTOR, "op_id": _KINDS["write_caution"][0]}
    )
    assert preview["error"]["message"] == "forbidden: 'preview_operation' requires a higher role"
    await _assert_nothing_stored_or_run()


# --- The walk over every REST route and every MCP tool ---------------------------------

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_PATH_PARAM = re.compile(r"\{[^}]+\}")


def _role_checks(route: APIRoute) -> list[tuple[TenantRole, bool]]:
    """The ``require_role`` checks of *route*: (lowest role, reads only)."""
    found: list[tuple[TenantRole, bool]] = []
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        pending.extend(dependant.dependencies)
        call = dependant.call
        if getattr(call, "__qualname__", "") != "require_role.<locals>._checker":
            continue
        nonlocals = inspect.getclosurevars(call).nonlocals
        min_rank = nonlocals["min_rank"]
        roles = (TenantRole.READ_ONLY, TenantRole.OPERATOR, TenantRole.TENANT_ADMIN)
        found.append((roles[min_rank], bool(nonlocals["reads_only"])))
    return found


def _change_routes() -> list[tuple[str, str, APIRoute]]:
    """Every REST route of the app that does not only read, from the route table."""
    found = []
    for route in iter_routes(app.routes):
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        for method in sorted(route.methods - _READ_METHODS):
            found.append((method, _PATH_PARAM.sub(str(uuid.uuid4()), route.path), route))
    return found


def _refused_for_a_reader(response: httpx.Response) -> bool:
    """A 403 with the code and the sentence, or a 403 of a check that runs first.

    A person-only route refuses a machine login before the role check, with
    its own code.
    """
    if response.status_code != 403:
        return False
    body = response.json()
    return body == _REST_BODY or body.get("detail") != READER_CHANGE_CODE


async def _walk_rest(ac: httpx.AsyncClient, headers: dict[str, str]) -> list[str]:
    """Call every change route as a Reader; return what went wrong.

    A route goes wrong when the call stores an approval request or runs a
    stand-in, or when a route that needs the ``operator`` role lets the
    Reader in or answers ``insufficient_role`` without the sentence.
    """
    problems: list[str] = []
    for method, path, route in _change_routes():
        requests_before = await _count(ApprovalRequest)
        runs_before = len(_RUNS)
        response = await ac.request(method, path, json={}, headers=headers)
        if await _count(ApprovalRequest) != requests_before or len(_RUNS) != runs_before:
            problems.append(f"{method} {route.path}: a Reader stored or ran something")
        operator_floor = (TenantRole.OPERATOR, False) in _role_checks(route)
        if operator_floor and not _refused_for_a_reader(response):
            problems.append(f"{method} {route.path}: {response.status_code} {response.text[:120]}")
    return problems


@pytest.mark.usefixtures("kinds")
async def test_the_walk_over_every_rest_change_route(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client
    assert len(_change_routes()) > 100
    for login, headers in _reader_tokens(key).items():
        assert await _walk_rest(ac, headers) == [], login
    await _assert_nothing_stored_or_run()


@pytest.mark.usefixtures("kinds")
async def test_the_walk_finds_a_route_that_runs_a_change_for_a_reader(
    client: tuple[httpx.AsyncClient, Any],
) -> None:
    ac, key = client

    async def _unchecked_change() -> dict[str, str]:
        _RUNS.append("unchecked")
        return {"ran": "yes"}

    app.add_api_route("/api/v1/test-only/unchecked-change", _unchecked_change, methods=["POST"])
    try:
        problems = await _walk_rest(ac, _reader_tokens(key)["person"])
    finally:
        app.router.routes.pop()

    assert problems == ["POST /api/v1/test-only/unchecked-change: a Reader stored or ran something"]


def _change_tool(name: str) -> bool:
    """A write tool that needs the ``operator`` role.

    ``call_operation`` looks the operation up first: its answer to a call
    that names no operation is the plain one.
    """
    entry = get_tool(name)
    assert entry is not None
    defn = entry[0]
    return (
        name != "call_operation"
        and defn.required_role is TenantRole.OPERATOR
        and defn.op_class not in {"read", "audit_query"}
    )


@pytest.mark.parametrize("login", ["person_token", "service", "agent"])
@pytest.mark.usefixtures("kinds")
async def test_the_walk_over_every_mcp_tool(
    client: tuple[httpx.AsyncClient, Any], mcp_as: Callable[[Operator], None], login: str
) -> None:
    ac, _ = client
    mcp_as(_READERS[login])
    names = sorted(registered_tool_names())
    assert "call_operation" in names

    problems = []
    for name in names:
        body = await _mcp_call(ac, name, {})
        if await _count(ApprovalRequest) != 0 or _RUNS:
            problems.append(f"{name}: a Reader stored or ran something")
        if _change_tool(name) and body.get("error", {}).get("message") != _ANSWER:
            problems.append(f"{name}: {str(body)[:120]}")
        if name == "call_operation" and body["error"]["message"] != _PLAIN_MCP:
            problems.append(f"{name}: {str(body)[:120]}")

    assert problems == []


#: The ``POST`` routes that only read: their ``operator`` door is ``reads_only``.
_READ_ROUTES = (
    "/api/v1/operations/preview",
    "/api/v1/search_docs",
    "/api/v1/ask_docs",
    "/api/v1/read_docs",
    "/api/v1/retrieve",
    "/api/v1/audit/query",
    "/api/v1/audit/class-counts",
)


def test_the_rest_walk_reads_the_role_door_of_each_route() -> None:
    """The walk reads the ``operator`` door of each route, so its check can fail.

    The general call route has its own door, which looks the operation up.
    """
    routes = {route.path: route for _method, _path, route in _change_routes()}
    doors = {path: _role_checks(route) for path, route in routes.items()}
    assert (TenantRole.OPERATOR, False) in doors["/api/v1/broadcast/announce"]
    for path in _READ_ROUTES:
        assert doors[path] == [(TenantRole.OPERATOR, True)], path
    call_doors = [d.call for d in routes["/api/v1/operations/call"].dependant.dependencies]
    assert require_operator_for_call in call_doors
    assert doors["/api/v1/operations/call"] == []


@pytest.mark.parametrize("path", _READ_ROUTES)
async def test_a_reader_post_that_only_reads_gets_the_plain_answer(
    client: tuple[httpx.AsyncClient, Any], path: str
) -> None:
    ac, key = client

    response = await ac.post(
        path, json={"query": "made-up question"}, headers=_reader_tokens(key)["person"]
    )

    assert response.status_code == 403, response.text
    assert response.json() == {"detail": READER_CHANGE_CODE}
