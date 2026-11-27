# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the tests of ``own_approval`` in ``meho policy explain``.

The callers, the fixture operations, explain through the real route, the
real call (``dispatch``, with a preview first) and the real approve
(``approve_request``) of the stored request. All names and values are
made up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations.approval_queue import ApprovalError, approve_request
from meho_backplane.operations.dispatcher import dispatch
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import DEFAULT_TENANT_ID, mint_token

TENANT = uuid.UUID(DEFAULT_TENANT_ID)
CONNECTOR = "demo-1.x"
LEVELS = ("safe", "caution", "dangerous", "destructive")
#: A ``safe`` operation marked ``requires_approval``: it waits for every caller.
SAFE_MARKED = "safe-marked"
OPS = (*LEVELS, SAFE_MARKED)
#: Both reasons of the part when the approve reads the values of the call.
DEPENDS = "This depends on the values of the call. Explain asks without them."


@dataclass(frozen=True)
class Caller:
    name: str
    sub: str
    kind: PrincipalKind
    role: TenantRole
    web: bool = False
    platform_admin: bool = False
    approver: bool = False

    @property
    def machine(self) -> bool:
        return self.kind is not PrincipalKind.USER

    def operator(self, *, web: bool | None = None) -> Operator:
        return Operator(
            sub=self.sub,
            raw_jwt="<jwt>",
            tenant_id=TENANT,
            tenant_role=self.role,
            principal_kind=self.kind,
            platform_admin=self.platform_admin,
            client_id=self.sub if self.machine else None,
            web_session=self.web if web is None else web,
        )

    def token(self, key: Any) -> str:
        claims: dict[str, Any] = {"tenant_role": self.role.value}
        if self.machine:
            claims["principal_kind"] = self.kind.value
        if self.platform_admin:
            claims["platform_admin"] = True
        if self.approver:
            claims["approver"] = True
        return mint_token(key, sub=self.sub, tenant_id=str(TENANT), **claims)

    def post(
        self,
        client: TestClient,
        key: Any,
        op_id: str,
        *,
        connector_id: str = CONNECTOR,
        **extra: Any,
    ) -> Any:
        body: dict[str, Any] = {"connector_id": connector_id, "op_id": op_id, **extra}
        if "groups" not in body:
            body["me"] = True
            if not self.machine:
                body["via"] = "web" if self.web else "token"
        return client.post(
            "/api/v1/policy/explain",
            json=body,
            headers={"Authorization": f"Bearer {self.token(key)}"},
        )

    def ask(self, client: TestClient, key: Any, op_id: str, **extra: Any) -> dict[str, Any]:
        resp = self.post(client, key, op_id, **extra)
        assert resp.status_code == 200, resp.text
        return dict(resp.json())


def _person(name: str, sub: str, role: TenantRole, **kw: Any) -> Caller:
    return Caller(name, sub, PrincipalKind.USER, role, **kw)


OPERATOR_WEB = _person("Operator, web console", "op-web", TenantRole.OPERATOR, web=True)
OPERATOR_TOKEN = _person("Operator, token", "op-token", TenantRole.OPERATOR)
ADMIN_WEB = _person("Admin, web console", "adm-web", TenantRole.TENANT_ADMIN, web=True)
ADMIN_TOKEN = _person("Admin, token", "adm-token", TenantRole.TENANT_ADMIN)
PLATFORM_WEB = _person(
    "Platform admin, web console",
    "pa-web",
    TenantRole.TENANT_ADMIN,
    web=True,
    platform_admin=True,
)
PLATFORM_TOKEN = _person(
    "Platform admin, token", "pa-token", TenantRole.TENANT_ADMIN, platform_admin=True
)
AGENT = Caller("agent login", "agent-a", PrincipalKind.AGENT, TenantRole.OPERATOR)
SERVICE = Caller("service login", "svc-a", PrincipalKind.SERVICE, TenantRole.OPERATOR)

#: An Operator and an Admin, each in the web console and by token, and two machine logins.
CALLERS = (OPERATOR_WEB, OPERATOR_TOKEN, ADMIN_WEB, ADMIN_TOKEN, AGENT, SERVICE)
#: The same with a platform admin in the web console and by token.
ALL_CALLERS = (*CALLERS, PLATFORM_WEB, PLATFORM_TOKEN)


def switches(monkeypatch: pytest.MonkeyPatch, **values: str) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


def op(name: str) -> str:
    return f"demo.vm.{name}"


async def register(
    embedding: AsyncMock,
    op_id: str,
    level: str,
    *,
    requires_approval: bool = False,
    params: tuple[str, ...] = (),
) -> None:
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={
            "type": "object",
            "properties": {name: {"type": "string"} for name in params},
        },
        group_key="compute",
        when_to_use="Fixture group.",
        safety_level=level,  # type: ignore[arg-type]
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


async def register_levels(embedding: AsyncMock) -> None:
    for name in OPS:
        level = "safe" if name == SAFE_MARKED else name
        await register(
            embedding, op(name), level, requires_approval=name in ("destructive", SAFE_MARKED)
        )


async def call(
    operator: Operator,
    op_id: str,
    params: dict[str, Any] | None = None,
    *,
    connector_id: str = CONNECTOR,
    target: Any = None,
) -> Any:
    """The real call, with a preview first, as a person makes it."""
    values = params or {}
    preview = await preview_dispatch(
        operator=operator, connector_id=connector_id, op_id=op_id, target=target, params=values
    )
    return await dispatch(
        operator=operator,
        connector_id=connector_id,
        op_id=op_id,
        target=target,
        params=values,
        preview_hash=preview.get("preview_hash"),
    )


async def park(
    caller: Caller, op_id: str, params: dict[str, Any] | None = None, **kw: Any
) -> uuid.UUID:
    result = await call(caller.operator(), op_id, params, **kw)
    assert result.status == "awaiting_approval", (caller.name, op_id, result)
    return uuid.UUID(str(result.extras["approval_request_id"]))


@dataclass(frozen=True)
class Approve:
    """What the real approve did; the decision is rolled back."""

    accepted: bool
    error: ApprovalError | None = None

    @property
    def reason(self) -> str:
        if self.error is None:
            return ""
        return str(getattr(self.error, "reason_text", self.error))


async def approve(
    caller: Caller, request_id: uuid.UUID, *, web: bool, params: dict[str, Any] | None = None
) -> Approve:
    async with get_sessionmaker()() as session:
        try:
            await approve_request(
                session, request_id, operator=caller.operator(web=web), params=params or {}
            )
        except ApprovalError as exc:
            return Approve(False, exc)
        finally:
            await session.rollback()
    return Approve(True)


async def stored(request_id: uuid.UUID) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        session.expunge(row)
        return row


async def all_requests() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars())


@dataclass
class Row:
    """One caller and one operation: the part, and what the approve did."""

    caller: Caller
    op_id: str
    answer: str
    own: dict[str, Any] | None
    request_id: uuid.UUID | None = None
    done: dict[str, Approve] = field(default_factory=dict)

    @property
    def where(self) -> str:
        return f"{self.caller.name} / {self.op_id}"


async def row_of(
    client: TestClient,
    key: Any,
    caller: Caller,
    op_id: str,
    params: dict[str, Any] | None = None,
) -> Row:
    body = caller.ask(client, key, op_id)
    row = Row(caller, op_id, body["answer"], body.get("own_approval"))
    if row.answer == "wait":
        row.request_id = await park(caller, op_id, params)
        for way, web in (("web", True), ("token", False)):
            row.done[way] = await approve(caller, row.request_id, web=web, params=params)
    return row


def agrees(row: Row) -> list[str]:
    """Where the part says something else than the approve did."""
    if row.answer != "wait":
        return [] if row.own is None else [f"{row.where}: answer {row.answer}, part {row.own}"]
    if row.own is None:
        return [f"{row.where}: the call waits but own_approval is missing"]
    found = []
    for way, done in row.done.items():
        said, reason = row.own[way], row.own[f"{way}_reason"]
        if said is None:
            found.append(f"{row.where}: {way} is null")
        elif said != done.accepted:
            found.append(f"{row.where}: {way} says {said}, the approve did {done.accepted}")
        elif not done.accepted and not row.caller.machine and reason != done.reason:
            found.append(f"{row.where}: {way} reason {reason!r}, the approve {done.reason!r}")
    return found


def table(rows: list[Row]) -> str:
    def word(value: Any) -> str:
        return "null" if value is None else ("yes" if value else "no")

    lines = ["| Caller | Operation | Answer | part web / token | approve web / token |"]
    lines.append("|---|---|---|---|---|")
    for row in rows:
        part = done = "-"
        if row.own is not None:
            part = f"{word(row.own['web'])} / {word(row.own['token'])}"
        if row.done:
            done = f"{word(row.done['web'].accepted)} / {word(row.done['token'].accepted)}"
        lines.append(f"| {row.caller.name} | {row.op_id} | {row.answer} | {part} | {done} |")
    return "\n".join(lines)


def isolate_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test registers its own changes of rights; they go when the test ends."""
    from meho_backplane import op_classes
    from meho_backplane.operations import grant_approval, role_floors

    for module, name in (
        (grant_approval, "_RIGHTS_CHANGE_OPS"),
        (grant_approval, "_RIGHTS_CHANGE_CHECKS"),
        (grant_approval, "_GRANT_OPS"),
        (role_floors, "_REQUESTER_CHECKS"),
        (op_classes, "_PER_CALL_APPROVAL"),
    ):
        monkeypatch.setattr(module, name, type(getattr(module, name))(getattr(module, name)))
