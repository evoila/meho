# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Writes that change who may log in or reach something carry ``changes_rights``.

A call changes rights when it changes who or what may reach or use something
in the target system: a login, a group or role, an access list, or a
firewall rule. The operations below fit that sentence, so their stored rows
say ``changes_rights`` is true. Two of them delete a firewall object, so
they also carry ``hard_to_undo="deletes"``; the other ten carry no
``hard_to_undo``.

No rule of MEHO reads ``changes_rights`` yet. A whole call proves it: a
service login calls an operation with the mark through ``call_operation``,
under a standing grant or under a run approval, and the call runs at once,
with the switch ``CUSTOM_POLICY`` off and on.

An operation at the level ``destructive`` that deletes an object carries
``hard_to_undo="deletes"``. Its call answers stay the ones of the code
before the mark: a person's token and a service login with a standing grant
both wait for approval, with the switch off and on.

With the switch ``CUSTOM_POLICY`` on as well as off, the approval answers
of the twelve operations stay the ones in
``tests/snapshots/approval_table.json``: whether a call needs approval, and
why no standing grant or run approval covers it.

The database backup keeps no ``hard_to_undo`` mark, and its text says that
it overwrites an older backup at the same path.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.msad import MsadConnector
from meho_backplane.connectors.msad import ops_users as msad_ops_users
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.windows_dns import WindowsDnsConnector
from meho_backplane.connectors.windows_dns import ops_record as windns_ops_record
from meho_backplane.connectors.windows_dns import ops_record_remove_preview as windns_preview
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, RunApproval, ServicePrincipalGrant
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import reset_handler_cache
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.settings import get_settings
from tests.approval_table import build_rows, register_all_descriptors

_TABLE = Path(__file__).parent / "snapshots" / "approval_table.json"

_RIGHTS_WRITES = frozenset(
    {
        "msad.user.create",
        "msad.user.enable",
        "msad.user.disable",
        "msad.group.create",
        "msad.computer.join-prestage",
        "msad.computer.unjoin",
        "winsrv.localuser.create",
        "winsrv.localuser.set",
        "pfsense.filter.rule.delete",
        "pfsense.nat.delete",
        "pfsense.alias.member.remove",
        "pfsense.openvpn.csc.sync",
    }
)

#: The two of the twelve that delete a firewall object.
_RIGHTS_DELETES = frozenset({"pfsense.filter.rule.delete", "pfsense.nat.delete"})

#: Operations at the level ``destructive`` that delete an object.
_DELETES = frozenset(
    {
        "bind9.record.remove",
        "bind9.record.delete",
        "windns.record.remove",
        "pfsense.filter.rule.delete",
        "pfsense.nat.delete",
        "pfsense.alias.delete",
        "pfsense.route.static.delete",
        "pfsense.gateway.delete",
    }
)

_BACKUP = "mssql.backup.database"

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007168")
_SERVICE_SUB = "svc:rights-marks"
_RUN_REF = "run-rights-marks"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_the_stored_rows_say_the_call_changes_rights() -> None:
    rows = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id in _RIGHTS_WRITES
    ]
    assert sorted(row.op_id for row in rows) == sorted(_RIGHTS_WRITES)
    for row in rows:
        assert row.changes_rights is True, row.op_id
        expected = "deletes" if row.op_id in _RIGHTS_DELETES else None
        assert row.hard_to_undo == expected, row.op_id
        assert row.safety_level != "safe", row.op_id


@pytest.mark.asyncio
async def test_the_stored_rows_of_a_delete_say_deletes() -> None:
    rows = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id in _DELETES
    ]
    assert sorted(row.op_id for row in rows) == sorted(_DELETES)
    for row in rows:
        assert row.hard_to_undo == "deletes", row.op_id
        assert row.safety_level == "destructive", row.op_id
        assert row.requires_approval is True, row.op_id


@pytest.mark.asyncio
@pytest.mark.parametrize("switch", ["false", "true"])
async def test_the_approval_answers_stay_the_same_with_the_switch_off_or_on(
    monkeypatch: pytest.MonkeyPatch, switch: str
) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", switch)
    get_settings.cache_clear()
    expected = [
        row
        for row in json.loads(_TABLE.read_text(encoding="utf-8"))
        if row["op_id"] in _RIGHTS_WRITES
    ]
    actual = [
        row
        for row in build_rows(await register_all_descriptors())
        if row["op_id"] in _RIGHTS_WRITES
    ]
    assert len(expected) == len(_RIGHTS_WRITES)
    assert actual == expected


@pytest.mark.asyncio
async def test_the_backup_has_no_mark_and_says_it_overwrites_an_older_backup() -> None:
    (row,) = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id == _BACKUP
    ]
    assert row.hard_to_undo is None
    assert row.llm_instructions is not None
    assert "overwrites an older backup at the same path" in str(row.llm_instructions["when_to_use"])


# ---------------------------------------------------------------------------
# Whole calls through ``call_operation``
# ---------------------------------------------------------------------------

#: Each call of a stand-in handler. A typed handler is a module-level
#: function, so the calls go to a module-level list that each test clears.
_HANDLER_CALLS: list[str] = []


async def _msad_user_enable(
    connector: Any, target: Any, params: dict[str, Any], operator: Operator | None = None
) -> dict[str, Any]:
    _HANDLER_CALLS.append("msad.user.enable")
    return {"identity": params["identity"], "action": "enable", "ok": True}


async def _windns_read_records(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """The record read of the delete's preview: one value at the name."""
    return {"rows": [{"type": "A", "rdata": "192.0.2.10"}]}


async def _windns_record_remove(
    connector: Any, target: Any, params: dict[str, Any], operator: Operator | None = None
) -> dict[str, Any]:
    _HANDLER_CALLS.append("windns.record.remove")
    return {"zone": params["zone"], "name": params["name"], "type": params["type"]}


@pytest.fixture
def handler_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """The two handlers swapped for stand-ins, and the two connectors registered.

    The swap comes after the stored rows are written, so the rows keep the
    real handler path; the dispatcher then finds the stand-in under it.
    """
    _HANDLER_CALLS.clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(product="msad", version="2022.x", impl_id="msad-ssh", cls=MsadConnector)
    register_connector_v2(
        product="windns", version="2016.x", impl_id="windns-ssh", cls=WindowsDnsConnector
    )
    monkeypatch.setattr(msad_ops_users, "msad_user_enable", _msad_user_enable)
    monkeypatch.setattr(windns_ops_record, "windows_dns_record_remove", _windns_record_remove)
    monkeypatch.setattr(windns_preview, "pwsh_run", _windns_read_records)
    yield _HANDLER_CALLS
    _HANDLER_CALLS.clear()
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


def _caller(kind: PrincipalKind) -> Operator:
    return Operator(
        sub=_SERVICE_SUB if kind is PrincipalKind.SERVICE else "person-rights-marks",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _seed_target(product: str, impl_id: str) -> tuple[str, uuid.UUID]:
    """A target of *product*; the name for the call, the id for a grant."""
    name = f"{product}-rights-marks"
    target_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            TargetORM(
                id=target_id,
                tenant_id=_TENANT,
                name=name,
                aliases=[],
                product=product,
                host="host.test.invalid",
                port=22,
                fqdn=None,
                secret_ref=f"targets/test/{product}",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={},
                preferred_impl_id=impl_id,
                notes="seeded by test_rights_marks_of_login_and_firewall_writes",
            )
        )
        await session.commit()
    return name, target_id


async def _seed_grant(connector_id: str, op_id: str, target_id: uuid.UUID) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=op_id,
                connector_id=connector_id,
                target_id=target_id,
                reason="fixture: standing grant",
                created_by_sub="fixture-admin",
            )
        )
        await session.commit()


async def _seed_run_approval(connector_id: str, op_id: str, target_id: uuid.UUID) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                work_ref=_RUN_REF,
                entries=[
                    {"connector_id": connector_id, "op_id": op_id, "target_id": str(target_id)}
                ],
                reason="fixture: run approval",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
        await session.commit()


async def _waiting_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ApprovalRequest.op_id)))


_MSAD = "msad-ssh-2022.x"
_ENABLE = "msad.user.enable"
_WINDNS = "windns-ssh-2016.x"
_REMOVE = "windns.record.remove"


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_on", [False, True], ids=["switch_off", "switch_on"])
@pytest.mark.parametrize("cover", ["standing_grant", "run_approval"])
async def test_a_grant_or_a_run_approval_covers_a_call_that_changes_rights(
    monkeypatch: pytest.MonkeyPatch, handler_calls: list[str], switch_on: bool, cover: str
) -> None:
    """A ``caution`` operation with ``changes_rights`` and a service login's grant or run approval.

    With the switch off, the call runs at once. With ``CUSTOM_POLICY`` on, the
    fixed rule ``target_rights_waits`` holds: the call waits for a person.
    """
    _switch(monkeypatch, switch_on)
    (row,) = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id == _ENABLE
    ]
    assert (row.safety_level, row.changes_rights, row.requires_approval) == (
        "caution",
        True,
        False,
    )
    target, target_id = await _seed_target("msad", "msad-ssh")
    arguments: dict[str, Any] = {
        "connector_id": _MSAD,
        "op_id": _ENABLE,
        "target": target,
        "params": {"identity": "jdoe"},
    }
    if cover == "standing_grant":
        await _seed_grant(_MSAD, _ENABLE, target_id)
    else:
        await _seed_run_approval(_MSAD, _ENABLE, target_id)
        arguments["work_ref"] = _RUN_REF

    answer = await call_operation(_caller(PrincipalKind.SERVICE), arguments)

    if switch_on:
        assert answer["status"] == "awaiting_approval", answer
        assert handler_calls == []
        assert await _waiting_ops() == [_ENABLE]
        return
    assert answer["status"] == "ok", answer
    assert handler_calls == [_ENABLE]
    assert await _waiting_ops() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("switch_on", [False, True], ids=["switch_off", "switch_on"])
@pytest.mark.parametrize(
    "kind", [PrincipalKind.USER, PrincipalKind.SERVICE], ids=["person", "service"]
)
async def test_a_delete_with_the_mark_waits_for_a_person_as_before(
    monkeypatch: pytest.MonkeyPatch,
    handler_calls: list[str],
    switch_on: bool,
    kind: PrincipalKind,
) -> None:
    """The call answers of a delete are the same as before it carried ``deletes``."""
    _switch(monkeypatch, switch_on)
    (row,) = [
        row
        for row in await register_all_descriptors()
        if row.tenant_id is None and row.op_id == _REMOVE
    ]
    assert row.hard_to_undo == "deletes"
    target, target_id = await _seed_target("windns", "windns-ssh")
    if kind is PrincipalKind.SERVICE:
        await _seed_grant(_WINDNS, _REMOVE, target_id)

    arguments: dict[str, Any] = {
        "connector_id": _WINDNS,
        "op_id": _REMOVE,
        "target": target,
        "params": {"zone": "example.test", "name": "www", "type": "A"},
    }
    preview = await preview_operation(_caller(kind), dict(arguments))
    arguments["preview_hash"] = preview["preview_hash"]
    answer = await call_operation(_caller(kind), arguments)

    assert answer["status"] == "awaiting_approval", answer
    assert answer["extras"]["error_code"] == "awaiting_approval", answer
    assert handler_calls == []
    assert await _waiting_ops() == [_REMOVE]
