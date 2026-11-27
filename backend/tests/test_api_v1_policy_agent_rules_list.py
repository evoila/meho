# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The per-role list of an import plan (``runs_at_once``), read as dispatch reads it.

Each case asks for the import preview with a tenant admin's token
(``POST /api/v1/policy/import?preview=true``) and reads, for each role, the
operations that start to run at once for a personal agent.

* The list uses the values a call through dispatch by a login of the tenant
  gets: the tenant's own on/off choice, an admin's per-operation edit (the
  risk level and per-call approval), and the tenant's own copy of a
  connector in place of the built-in row. Each case compares the list with
  what :func:`~meho_backplane.operations._lookup.lookup_descriptor` finds.
* An operation that always waits is in no role's list, for each kind of
  reason. An operation with the plain "always ask" mark is listed only under
  a rule that allows ``dangerous``.
* The list holds only what the import changes: an edit that leaves the
  rules the same lists none.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane import op_classes
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    OperationPolicyOverride,
    TenantOperationSetting,
)
from meho_backplane.operations import grant_approval
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.typed_register import register_typed_operation

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    switch_on_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_OP = "inslim.vm.restart"
_TENANT_B = uuid.UUID("7c0ffee0-0000-4000-8000-00000000b0b0")
_KEY = {"product": "inslim", "version": "1.x", "impl_id": "inslim"}


def _file(up_to: str = "dangerous", *, roles: str = "Admin, Operator") -> str:
    return (
        "policy: v1\nclassifications:\n  - {match: {connector: inslim-1.x}, category: network}\n"
        "agents:\n  - name: helper\n    caller: personal_agent\n"
        f"    roles: [{roles}]\n    up_to: {up_to}\n"
    )


_KEYPAIR = make_rsa_keypair("kid-agents-list")


def _preview(client: TestClient, content: str) -> dict[str, list[dict[str, str]]]:
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        resp = h.post(client, "import", a.tenant_admin(_KEYPAIR), content, preview="true")
    assert resp.status_code == 200, resp.text
    lists: dict[str, list[dict[str, str]]] = resp.json()["diff"]["runs_at_once"]
    return lists


async def _register(op_id: str, level: str = "caution", **extra: Any) -> None:
    await register_typed_operation(
        **_KEY,
        op_id=op_id,
        handler=h._handler,
        summary="Fixture operation.",
        description="Fixture operation.",
        parameter_schema={"type": "object", "properties": {}},
        group_key="compute",
        when_to_use="Fixture group.",
        safety_level=level,  # type: ignore[arg-type]
        **extra,
    )


async def _override(**values: Any) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(OperationPolicyOverride(**_KEY, op_id=_OP, set_by_sub="admin-a", **values))


async def _turn(tenant_id: uuid.UUID, enabled: bool) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TenantOperationSetting(
                tenant_id=tenant_id,
                **_KEY,
                op_id=_OP,
                enabled=enabled,
                set_by_sub="admin-a",
                set_at=datetime.now(UTC),
            )
        )


async def _install_off() -> None:
    async with get_sessionmaker()() as session, session.begin():
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == _OP, EndpointDescriptor.tenant_id.is_(None))
            .values(is_enabled=False)
        )


async def _tenant_copy() -> None:
    """Tenant A's own copy of the connector: the same connector id and operation ids."""
    async with get_sessionmaker()() as session, session.begin():
        rows = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "inslim", EndpointDescriptor.tenant_id.is_(None)
                )
            )
        ).scalars()
        for row in list(rows):
            values = {
                column.key: getattr(row, column.key)
                for column in EndpointDescriptor.__table__.columns
                if column.key not in ("id", "tenant_id")
            }
            session.add(EndpointDescriptor(**values, tenant_id=h.TENANT))


async def _nothing() -> None:
    return None


async def _dispatch_sees(op_id: str) -> tuple[str, bool] | None:
    """What a call by a tenant A login finds: the level and per-call approval, or nothing."""
    row = await lookup_descriptor(tenant_id=h.TENANT, **_KEY, op_id=op_id)
    return None if row is None else (row.safety_level, bool(row.requires_approval))


def _listed(lists: dict[str, list[dict[str, str]]], op_id: str) -> dict[str, list[str]]:
    return {
        role: [item["risk"] for item in items if item["op_id"] == op_id]
        for role, items in lists.items()
    }


_Setup = Callable[[], Coroutine[Any, Any, None]]

_CASES: dict[str, tuple[_Setup, bool]] = {
    "no-edit": (_nothing, True),
    "raised-to-destructive": (lambda: _override(safety_level="destructive"), False),
    "per-call-approval-on": (lambda: _override(requires_approval=True), False),
    "tenant-a-turns-it-off": (lambda: _turn(h.TENANT, False), False),
    "tenant-b-turns-it-off": (lambda: _turn(_TENANT_B, False), True),
    "tenant-a-copy-of-the-connector": (_tenant_copy, True),
}


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize("case", list(_CASES))
async def test_the_list_reads_what_dispatch_reads_for_the_tenant(
    client: TestClient, embedding: AsyncMock, case: str
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    setup, listed = _CASES[case]
    await setup()

    lists = _preview(client, _file())

    shown = _listed(lists, _OP)
    assert shown == (
        {"Reader": [], "Operator": ["caution"], "Admin": ["caution"]}
        if listed
        else {"Reader": [], "Operator": [], "Admin": []}
    )
    seen = await _dispatch_sees(_OP)
    assert listed == (seen is not None and seen == ("caution", False))


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_an_operation_off_for_the_install_and_on_for_the_tenant_is_listed(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _install_off()
    assert _listed(_preview(client, _file()), _OP)["Operator"] == []
    assert await _dispatch_sees(_OP) is None

    await _turn(h.TENANT, True)
    assert _listed(_preview(client, _file()), _OP)["Operator"] == ["caution"]
    assert await _dispatch_sees(_OP) == ("caution", False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
@pytest.mark.parametrize(
    ("op_id", "extra"),
    [
        ("inslim.vm.wipe", {"level": "destructive"}),
        ("inslim.vm.poweroff", {"hard_to_undo": "stops"}),
        ("inslim.vm.console", {"root_shell": True}),
        ("inslim.vm.grant", {"rights_change": True}),
        ("inslim.vm.addmember", {"per_call": True}),
        ("inslim.vm.batch", {"composite": True}),
        ("inslim.vm.pinned", {"always_wait": True}),
        ("inslim.vm.delete", {}),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
async def test_an_operation_that_always_waits_is_in_no_list(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    op_id: str,
    extra: dict[str, Any],
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    level = extra.get("level", "caution")
    await _register(
        op_id,
        level,
        hard_to_undo=extra.get("hard_to_undo"),
        requires_approval=level == "destructive",
    )
    if extra.get("root_shell"):
        monkeypatch.setattr(op_classes, "_ROOT_SHELL", {op_id})
    if extra.get("rights_change"):
        monkeypatch.setattr(grant_approval, "_RIGHTS_CHANGE_OPS", {op_id})
    if extra.get("per_call"):
        monkeypatch.setattr(op_classes, "_PER_CALL_APPROVAL", {op_id})
    if extra.get("composite"):
        async with get_sessionmaker()() as session, session.begin():
            await session.execute(
                update(EndpointDescriptor)
                .where(EndpointDescriptor.op_id == op_id)
                .values(source_kind="composite")
            )
    pinned = (
        f"  - {{match: {{op: {op_id}}}, always_wait: true}}\n" if extra.get("always_wait") else ""
    )
    content = _file(roles="Reader, Operator, Admin").replace(
        "category: network}\n", "category: network}\n" + pinned
    )

    lists = _preview(client, content)

    assert _listed(lists, op_id) == {"Reader": [], "Operator": [], "Admin": []}
    assert _listed(lists, _OP)["Operator"] == ["caution"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_plain_always_ask_mark_is_listed_only_under_a_rule_that_allows_dangerous(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _register("inslim.vm.resize", requires_approval=True)

    caution = _preview(client, _file("caution"))
    dangerous = _preview(client, _file("dangerous"))

    assert _listed(caution, "inslim.vm.resize") == {"Reader": [], "Operator": [], "Admin": []}
    assert _listed(dangerous, "inslim.vm.resize") == {
        "Reader": [],
        "Operator": [],
        "Admin": ["dangerous"],
    }


# ---------------------------------------------------------------------------
# Only what the import changes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_the_list_holds_only_what_the_import_changes(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    await _register("inslim.vm.migrate", "dangerous")
    await a.set_limit("dangerous")
    stored = _file("caution")
    assert (await a.dispatch_tenant_apply(h.person("admin-a"), stored)).status == "ok"

    described = _preview(
        client, stored.replace("policy: v1\n", "policy: v1\ndescription: new words\n")
    )
    widened = _preview(client, _file("dangerous"))
    reader = stored + (
        "  - name: reader-helper\n    caller: personal_agent\n"
        "    roles: [Reader]\n    up_to: safe\n"
    )
    for_reader = _preview(client, reader)

    assert described == {"Reader": [], "Operator": [], "Admin": []}
    assert {role: a.inslim(items) for role, items in widened.items()} == {
        "Reader": [],
        "Operator": [],
        "Admin": ["inslim.vm.migrate"],
    }
    assert for_reader == {"Reader": [], "Operator": [], "Admin": []}
