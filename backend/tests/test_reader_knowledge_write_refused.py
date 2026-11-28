# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A login with the Reader role changes nothing in the knowledge store.

Every call goes through the real MCP route ``POST /mcp`` (``tools/call`` of
``add_to_knowledge``), the real handler and the knowledge store. A Reader
gets the code ``insufficient_role`` and the Reader sentence, with or
without the ``approver`` flag, and the entry keeps its revision, its text
and its metadata. A login with the Operator or Admin role keeps its
answers. The hosted agent run case goes through the run's write tool.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport
from pydantic_ai import ModelRetry

from meho_backplane.agent.meta_tool import MetaToolSpec
from meho_backplane.agent.tool_audit import ToolRefusedRetry
from meho_backplane.agent.toolset import _refuse_role_below_floor
from meho_backplane.agent.toolset_discovery import build_write_meta_tools
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.kb.schemas import KbEntry
from meho_backplane.kb.service import KbService
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.operations.role_floors import READER_CHANGE_CODE, READER_CHANGE_SENTENCE
from meho_backplane.settings import get_settings
from tests._kb_embedding_stub import fake_embedding_service

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000aa01")
_SHARED_OWNER = uuid.UUID("00000000-0000-0000-0000-00000000aa02")
_REFUSAL = f"{READER_CHANGE_CODE}: {READER_CHANGE_SENTENCE}"

_OTHER = "made-up-runbook"
_OWN = "made-up-notes"
_SHARED = "made-up-shared"
_NEW = "made-up-new"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KB_SHARED_TENANT_IDS", str(_SHARED_OWNER))
    get_settings.cache_clear()
    with patch(
        "meho_backplane.retrieval.indexer.get_embedding_service",
        return_value=fake_embedding_service(),
    ):
        yield
    get_settings.cache_clear()


def _login(
    role: TenantRole, kind: PrincipalKind = PrincipalKind.USER, *, approver: bool = False
) -> Operator:
    return Operator(
        sub=f"caller-{kind.value}",
        name="Caller",
        email=None,
        raw_jwt="<test-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        client_id=None if kind is PrincipalKind.USER else f"client-{kind.value}",
        approver=approver,
    )


async def _seed(caller: Operator) -> None:
    """The other person's entry, the caller's own entry and a shared entry."""
    service = KbService()
    await service.create_entry(_TENANT, _OTHER, "first text", actor_sub="person-owner-1")
    await service.create_entry(_TENANT, _OWN, "own text", actor_sub=caller.sub)
    await service.create_entry(_SHARED_OWNER, _SHARED, "shared text", {"shared": True})


async def _entry(slug: str) -> KbEntry | None:
    return await KbService().get_entry(_TENANT, slug)


def _state(entry: KbEntry | None) -> tuple[Any, ...] | None:
    if entry is None:
        return None
    return (entry.revision_id, entry.body, entry.metadata)


@pytest.fixture
async def mcp() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://testserver",
    ) as ac:
        yield ac
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _add(ac: httpx.AsyncClient, caller: Operator, arguments: dict[str, Any]) -> Any:
    async def _verify() -> Operator:
        return caller

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify
    response = await ac.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "add_to_knowledge", "arguments": arguments},
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _arguments(slug: str, body: str, **extra: Any) -> dict[str, Any]:
    """The call, with ``expected_revision`` from a read made just before it."""
    arguments: dict[str, Any] = {"slug": slug, "body": body, **extra}
    entry = await _entry(slug)
    if entry is not None:
        arguments["expected_revision"] = str(entry.revision_id)
    return arguments


_READER_CASES: dict[str, tuple[PrincipalKind, bool, str, dict[str, Any]]] = {
    "R1": (PrincipalKind.USER, False, _NEW, {}),
    "R2": (PrincipalKind.USER, False, _OTHER, {}),
    "R3": (PrincipalKind.USER, True, _NEW, {}),
    "R4": (PrincipalKind.USER, True, _OWN, {}),
    "R5": (PrincipalKind.USER, True, _OTHER, {}),
    "R6": (PrincipalKind.USER, True, _SHARED, {}),
    "R7": (PrincipalKind.USER, True, _OTHER, {"metadata": {"status": "confirmed"}}),
    "R8": (PrincipalKind.USER, True, _OTHER, {"dry_run": True}),
    "R9": (PrincipalKind.AGENT, True, _OTHER, {}),
    "R10": (PrincipalKind.SERVICE, True, _OTHER, {}),
}


@pytest.mark.parametrize("case", list(_READER_CASES))
async def test_a_reader_login_changes_nothing_in_the_knowledge_store(
    mcp: httpx.AsyncClient, case: str
) -> None:
    kind, approver, slug, extra = _READER_CASES[case]
    reader = _login(TenantRole.READ_ONLY, kind, approver=approver)
    await _seed(reader)
    before = {s: _state(await _entry(s)) for s in (_OTHER, _OWN, _SHARED, _NEW)}

    body = await _add(mcp, reader, await _arguments(slug, "reader text", **extra))

    assert body["error"]["code"] == INVALID_PARAMS, body
    assert body["error"]["message"] == _REFUSAL
    assert {s: _state(await _entry(s)) for s in before} == before
    assert before[_NEW] is None


async def test_a_hosted_run_of_a_reader_with_the_approver_flag_changes_no_entry() -> None:
    """R11: the run's write tool ``add_to_knowledge`` refuses the run's Reader."""
    reader = _login(TenantRole.READ_ONLY, approver=True)
    await _seed(reader)
    before = _state(await _entry(_OTHER))
    (tool,) = [spec for spec in build_write_meta_tools() if spec.name == "add_to_knowledge"]

    with pytest.raises(ModelRetry) as refused:
        await tool.handler(reader, await _arguments(_OTHER, "reader text"))
    with pytest.raises(ToolRefusedRetry) as at_the_tool:
        _refuse_role_below_floor(tool, reader)

    assert str(refused.value) == f"{_REFUSAL} Do not retry it."
    assert str(at_the_tool.value) == f"{_REFUSAL} Do not retry it."
    assert at_the_tool.value.reason_code == READER_CHANGE_CODE
    assert _state(await _entry(_OTHER)) == before


def test_add_to_knowledge_does_not_open_for_the_approver_flag() -> None:
    entry = get_tool("add_to_knowledge")
    assert entry is not None
    assert entry[0].required_role is TenantRole.OPERATOR
    assert entry[0].approver_may_call is False


def test_a_read_tool_of_a_hosted_run_keeps_the_plain_refusal() -> None:
    reader = _login(TenantRole.READ_ONLY)
    read = MetaToolSpec(
        name="search_knowledge",
        handler=build_write_meta_tools()[0].handler,
        description="Stand-in read.",
        parameter_schema={"type": "object"},
        required_role=TenantRole.OPERATOR,
    )

    with pytest.raises(ToolRefusedRetry) as refused:
        _refuse_role_below_floor(read, reader)

    assert (
        str(refused.value)
        == "forbidden: 'search_knowledge' requires a higher role. Do not retry it."
    )


# --- A login with the Operator or Admin role keeps its answers ------------------------

#: case -> (kind, role, approver, slug, extra arguments, the stored text after,
#: the refusal reason or None).
_KEEPS: dict[str, tuple[PrincipalKind, TenantRole, bool, str, dict[str, Any], str, str | None]] = {
    "O1": (PrincipalKind.USER, TenantRole.OPERATOR, False, _NEW, {}, "new text", None),
    # A person with the Operator role can decide approvals: a reviewer.
    "O2": (PrincipalKind.USER, TenantRole.OPERATOR, False, _OTHER, {}, "new text", None),
    "O3": (PrincipalKind.USER, TenantRole.OPERATOR, True, _OTHER, {}, "new text", None),
    "O4": (
        PrincipalKind.USER,
        TenantRole.TENANT_ADMIN,
        False,
        _OTHER,
        {"metadata": {"status": "confirmed"}},
        "new text",
        None,
    ),
    "O5": (PrincipalKind.AGENT, TenantRole.OPERATOR, False, _OWN, {}, "new text", None),
    "O6": (
        PrincipalKind.AGENT,
        TenantRole.OPERATOR,
        False,
        _OTHER,
        {},
        "first text",
        "entry_not_owned",
    ),
    "O7": (
        PrincipalKind.SERVICE,
        TenantRole.OPERATOR,
        False,
        _OTHER,
        {},
        "first text",
        "entry_not_owned",
    ),
}


@pytest.mark.parametrize("case", list(_KEEPS))
async def test_an_operator_or_admin_login_keeps_its_knowledge_answer(
    mcp: httpx.AsyncClient, case: str
) -> None:
    kind, role, approver, slug, extra, stored, reason = _KEEPS[case]
    caller = _login(role, kind, approver=approver)
    await _seed(caller)

    body = await _add(mcp, caller, await _arguments(slug, "new text", **extra))

    entry = await _entry(slug)
    assert entry is not None
    assert entry.body == stored
    if reason is None:
        assert "error" not in body, body
    else:
        assert body["error"]["data"]["reason"] == reason, body
    if case == "O4":
        assert entry.metadata["status"] == "confirmed"
