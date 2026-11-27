# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``own_approval`` of ``POST /api/v1/policy/explain`` says what the approve does.

For each caller, each risk level and both settings of
``APPROVAL_ALLOW_SELF_APPROVAL``: explain through the real route, then the
real call, then the real approve of that request by the same login, once
with a click in the web console and once with a token. The approve must do
what the part said: accepted, or refused with the same reason.

With ``CUSTOM_POLICY`` off the answer has no ``own_approval`` key.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import respx
import structlog
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import ACTOR_SUB_KEY
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.main import app
from meho_backplane.operations import grant_approval
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    register_preview_builder,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import (
    ApprovalError,
    NonHumanApprovalError,
    approve_request,
)
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.explain import CallQuestion, explain_call
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_CONNECTOR = "ownappr-1.x"
_LEVELS = ("safe", "caution", "dangerous", "destructive")
#: A ``safe`` operation marked ``requires_approval``: it waits for every caller.
_SAFE_MARKED = "safe-marked"
_OPS = (*_LEVELS, _SAFE_MARKED)
_NOT_A_PERSON = "Only a person can approve. AI tools and other machine logins never approve."

_ADMINS_ONLY: dict[str, Any] = {
    "policy": "v1",
    "routing": [{"match": {"connector": "ownappr-*"}, "approvers": ["Admins"]}],
}

#: The keys of the explain answer on main, before ``own_approval``.
_MAIN_KEYS = {
    "status",
    "engine",
    "connector_id",
    "op_id",
    "target",
    "file",
    "asker",
    "answer",
    "decided_by",
    "approvers",
    "today",
    "evaluator",
    "classification",
    "request",
    "not_checked",
    "error",
    "extras",
}


@dataclass(frozen=True)
class _Caller:
    name: str
    sub: str
    kind: PrincipalKind
    role: TenantRole
    web: bool = False

    def operator(self, *, web: bool | None = None) -> Operator:
        return Operator(
            sub=self.sub,
            raw_jwt="<jwt>",
            tenant_id=_TENANT,
            tenant_role=self.role,
            principal_kind=self.kind,
            client_id=None if self.kind is PrincipalKind.USER else self.sub,
            web_session=self.web if web is None else web,
        )

    def ask(self, client: TestClient, key: Any, op_id: str) -> dict[str, Any]:
        claims: dict[str, Any] = {"tenant_role": self.role.value}
        if self.kind is not PrincipalKind.USER:
            claims["principal_kind"] = self.kind.value
        token = mint_token(key, sub=self.sub, tenant_id=str(_TENANT), **claims)
        body: dict[str, Any] = {"connector_id": _CONNECTOR, "op_id": op_id, "me": True}
        if self.kind is PrincipalKind.USER:
            body["via"] = "web" if self.web else "token"
        resp = client.post(
            "/api/v1/policy/explain", json=body, headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        return dict(resp.json())


_CALLERS = (
    _Caller("Operator, web console", "op-web", PrincipalKind.USER, TenantRole.OPERATOR, web=True),
    _Caller("Operator, token", "op-token", PrincipalKind.USER, TenantRole.OPERATOR),
    _Caller("Admin, web console", "adm-web", PrincipalKind.USER, TenantRole.TENANT_ADMIN, web=True),
    _Caller("Admin, token", "adm-token", PrincipalKind.USER, TenantRole.TENANT_ADMIN),
    _Caller("agent login", "agent-a", PrincipalKind.AGENT, TenantRole.OPERATOR),
    _Caller("service login", "svc-a", PrincipalKind.SERVICE, TenantRole.OPERATOR),
)


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    install_fake_vault(monkeypatch)
    yield TestClient(app)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


def _op(level: str) -> str:
    return f"ownappr.vm.{level}"


async def _blast_radius(ctx: PreviewContext) -> dict[str, Any]:
    """A destructive call waits only with this block on its preview."""
    return {
        "blast_radius": {
            "object": {"kind": "vm", "id": "vm-1"},
            "children": [],
            "irreversibility": "permanent",
        }
    }


@pytest.fixture(autouse=True)
def _builders() -> Iterator[None]:
    saved = dict(_PREVIEW_BUILDERS)
    register_preview_builder(_op("destructive"), _blast_radius)
    yield
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(saved)


async def _setup(embedding: AsyncMock) -> None:
    for name in _OPS:
        level = "safe" if name == _SAFE_MARKED else name
        await register_typed_operation(
            product="ownappr",
            version="1.x",
            impl_id="ownappr",
            op_id=_op(name),
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="compute",
            when_to_use="Fixture group.",
            safety_level=level,  # type: ignore[arg-type]
            requires_approval=name in ("destructive", _SAFE_MARKED),
            embedding_service=embedding,
        )


async def _park(caller: _Caller, op_id: str) -> uuid.UUID:
    operator = caller.operator()
    preview = await preview_dispatch(
        operator=operator, connector_id=_CONNECTOR, op_id=op_id, target=None, params={}
    )
    result = await dispatch(
        operator=operator,
        connector_id=_CONNECTOR,
        op_id=op_id,
        target=None,
        params={},
        preview_hash=preview.get("preview_hash"),
    )
    assert result.status == "awaiting_approval", (caller.name, op_id, result)
    return uuid.UUID(str(result.extras["approval_request_id"]))


async def _approve(caller: _Caller, request_id: uuid.UUID, *, web: bool) -> tuple[bool, str]:
    """What the real approve does; the decision is rolled back."""
    async with get_sessionmaker()() as session:
        try:
            await approve_request(session, request_id, operator=caller.operator(web=web))
        except NonHumanApprovalError:
            # The approve gives no plain text here; the sign's text says it.
            return False, _NOT_A_PERSON
        except ApprovalError as exc:
            return False, str(getattr(exc, "reason_text", exc))
        finally:
            await session.rollback()
    return True, ""


@dataclass(frozen=True)
class _Row:
    caller: str
    level: str
    answer: str
    own: dict[str, Any] | None
    web: tuple[bool, str] | None
    token: tuple[bool, str] | None


async def _rows(
    client: TestClient,
    key: Any,
    callers: tuple[_Caller, ...] = _CALLERS,
    ops: tuple[str, ...] = _OPS,
) -> list[_Row]:
    rows: list[_Row] = []
    for caller in callers:
        for level in ops:
            body = caller.ask(client, key, _op(level))
            own = body.get("own_approval")
            if body["answer"] != "wait":
                rows.append(_Row(caller.name, level, body["answer"], own, None, None))
                continue
            request_id = await _park(caller, _op(level))
            web = await _approve(caller, request_id, web=True)
            token = await _approve(caller, request_id, web=False)
            rows.append(_Row(caller.name, level, "wait", own, web, token))
    return rows


def _mismatches(rows: list[_Row]) -> list[str]:
    found: list[str] = []
    for row in rows:
        where = f"{row.caller} / {row.level}"
        if row.answer != "wait":
            if row.own is not None:
                found.append(f"{where}: answer {row.answer} but own_approval {row.own}")
            continue
        if row.own is None:
            found.append(f"{where}: the call waits but own_approval is missing")
            continue
        assert row.web is not None and row.token is not None
        for way, (accepted, reason) in (("web", row.web), ("token", row.token)):
            if row.own[way] != accepted:
                found.append(f"{where}: {way} says {row.own[way]}, the approve did {accepted}")
            elif not accepted and row.own[f"{way}_reason"] != reason:
                found.append(f"{where}: {way} reason differs: {row.own[f'{way}_reason']!r}")
    return found


def _table(rows: list[_Row]) -> str:
    lines = ["| Caller | Level | Answer | web | token |", "|---|---|---|---|---|"]
    for row in rows:
        web = token = "-"
        if row.own is not None:
            web = "yes" if row.own["web"] else "no"
            token = "yes" if row.own["token"] else "no"
        lines.append(f"| {row.caller} | {row.level} | {row.answer} | {web} | {token} |")
    return "\n".join(lines)


def _switches(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("break_glass", ["false", "true"])
async def test_own_approval_says_what_the_approve_does(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    break_glass: str,
) -> None:
    _switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL=break_glass)
    await _setup(embedding)
    key = make_rsa_keypair(f"kid-ownappr-{break_glass}")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key)
    print(f"\nAPPROVAL_ALLOW_SELF_APPROVAL={break_glass}\n{_table(rows)}")
    assert _mismatches(rows) == []
    waits = {row.caller for row in rows if row.answer == "wait"}
    assert waits == {caller.name for caller in _CALLERS}, rows
    by = {(row.caller, row.level): row for row in rows}
    # A call that runs at once has no part.
    assert by[("Operator, web console", "caution")].own is None
    # A machine login never approves.
    agent = by[("agent login", "dangerous")].own
    assert agent is not None and not agent["web"] and not agent["token"]
    # A caution call that waits only for its token: a click approves it.
    token_wait = by[("Operator, token", "caution")].own
    assert token_wait is not None and token_wait["web"] is True
    assert token_wait["token"] is (break_glass == "true")
    # Break-glass never covers dangerous or destructive.
    for level in ("dangerous", "destructive"):
        own = by[("Operator, token", level)].own
        assert own is not None and not own["web"] and not own["token"]
    # Break-glass covers a safe change.
    marked = by[("Operator, token", _SAFE_MARKED)].own
    assert marked is not None and marked["web"] is marked["token"] is (break_glass == "true")
    # A MEHO-Admin approves their own change at every level.
    admin = by[("Admin, token", "destructive")].own
    assert admin is not None and admin["web"] and admin["token"]


@pytest.mark.asyncio
async def test_own_approval_follows_the_routing(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The install's routing lets only Admins approve: an Operator's own approval is refused."""
    _switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="true")
    await _setup(embedding)
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=None, document=_ADMINS_ONLY, set_by_sub="admin-a"))
        await session.commit()
    key = make_rsa_keypair("kid-ownappr-routing")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key)
    assert _mismatches(rows) == []
    by = {(row.caller, row.level): row for row in rows}
    operator = by[("Operator, token", "caution")].own
    assert operator is not None and not operator["web"] and not operator["token"]
    assert "install:routing[0]" in operator["token_reason"]
    admin = by[("Admin, token", "caution")].own
    assert admin is not None and admin["web"] and admin["token"]


@pytest.mark.asyncio
async def test_own_approval_of_a_change_of_rights(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change of rights: only an admin, and only with a click in the web console."""
    _switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="true")
    monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {_op("dangerous")})
    await _setup(embedding)
    key = make_rsa_keypair("kid-ownappr-rights")
    callers = tuple(c for c in _CALLERS if c.kind is PrincipalKind.USER and not c.web)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key, callers, ("dangerous",))
    assert _mismatches(rows) == []
    by = {row.caller: row.own for row in rows}
    admin, operator = by["Admin, token"], by["Operator, token"]
    assert admin is not None and admin["web"] and not admin["token"]
    assert operator is not None and not operator["web"] and not operator["token"]


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["caution", "dangerous"])
async def test_a_hosted_run_reads_what_the_approve_does(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    """A person's login in a hosted agent run: the part says what the approve does."""
    _switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="false")
    await _setup(embedding)
    caller = _Caller("hosted run", "person-run", PrincipalKind.USER, TenantRole.OPERATOR)
    with structlog.contextvars.bound_contextvars(**{ACTOR_SUB_KEY: "agent:hosted"}):
        answer = await explain_call(
            CallQuestion(
                asker=caller.operator(), me=True, connector_id=_CONNECTOR, op_id=_op(level)
            )
        )
        assert answer.answer == "wait", answer
        request_id = await _park(caller, _op(level))
    own = answer.own_approval
    assert own is not None
    for way, (accepted, reason) in (
        ("web", await _approve(caller, request_id, web=True)),
        ("token", await _approve(caller, request_id, web=False)),
    ):
        assert getattr(own, way) is accepted, (way, own)
        assert getattr(own, f"{way}_reason") == reason, (way, own)
    # The run's call never waits only for its token, so no click approves it.
    assert not own.web and not own.token


@pytest.mark.asyncio
async def test_the_check_finds_a_part_that_differs_from_the_approve(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the part forgets why a caution call waits, the comparison above fails."""
    _switches(monkeypatch, CUSTOM_POLICY="true", APPROVAL_ALLOW_SELF_APPROVAL="false")
    await _setup(embedding)
    key = make_rsa_keypair("kid-ownappr-broken")
    with (
        patch(
            "meho_backplane.policy.explain_own_approval.waits_only_for_token_call",
            return_value=False,
        ),
        respx.mock as r,
    ):
        mock_discovery_and_jwks(r, public_jwks(key))
        rows = await _rows(client, key)
    assert any("Operator, token / caution: web" in line for line in _mismatches(rows))


@pytest.mark.asyncio
async def test_with_the_switch_off_the_answer_has_no_new_key(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switches(monkeypatch, CUSTOM_POLICY="false", APPROVAL_ALLOW_SELF_APPROVAL="true")
    await _setup(embedding)
    key = make_rsa_keypair("kid-ownappr-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        for caller in _CALLERS:
            for level in _OPS:
                body = caller.ask(client, key, _op(level))
                assert set(body) == _MAIN_KEYS, (caller.name, level, sorted(body))
