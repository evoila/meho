# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the install's delete names only add to the built-in ones.

* The setting ``SERVICE_GRANT_DELETE_SHAPED_PATTERNS`` never removes a
  built-in delete name with the switch on, also when it is empty.
* Each part of MEHO that reads the setting sees the delete sign of an
  operation with a built-in delete name: with the switch on and a setting
  that leaves the name out, and with no setting at all.
* A service login calls an operation with a built-in delete name over MCP,
  under a standing grant, with a setting that lists other names: with the
  switch on the call waits for a person, the handler never runs, and the
  grant is not used. The same call of an operation with no delete sign
  runs at once.
* With the switch off, a setting with at least one name replaces the
  built-in names; an empty or blank one keeps them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    AuditLog,
    EndpointDescriptor,
    ServicePrincipalGrant,
    Tenant,
)
from meho_backplane.delete_names import DELETE_SHAPED_OP_ID_PATTERNS
from meho_backplane.flight_recorder.capture import _op_context_from
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations._handler_resolve import _HANDLER_CACHE
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations.governed_subops import classify_subop_grantability
from meho_backplane.operations.run_approval_schemas import RunApprovalCreate, RunApprovalEntry
from meho_backplane.operations.run_approvals import (
    RunApprovalService,
    RunApprovalValidationError,
)
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
    never_covered_reason,
)
from meho_backplane.operations.typed_register import (
    clear_typed_op_snapshot,
    register_typed_operation,
)
from meho_backplane.policy.fixed_rules import DELETE_WAITS, fixed_rule_for
from meho_backplane.policy.shadow import asked_of
from meho_backplane.redaction.flight_recorder.families import classify_body_exclusion
from meho_backplane.settings import _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS, get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_OTHER_NAMES = "acme.custom.nuke, POST:/acme/*/wipe"
_FIX = {"product": "dnfix", "version": "1.x", "impl_id": "dnfix"}
_CONNECTOR = "dnfix-1.x"
#: A ``caution`` operation whose only delete sign is the built-in ``*.delete``.
_DELETE_OP = "dnfix.vm.delete"
#: A ``caution`` operation with no delete sign.
_REBOOT_OP = "dnfix.vm.reboot"
_SERVICE_SUB = "svc:delete-names"


def _env(monkeypatch: pytest.MonkeyPatch, *, switch_on: bool, names: str | None) -> None:
    """Set the switch and the setting; ``names=None`` leaves the setting unset."""
    if switch_on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    if names is None:
        monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    else:
        monkeypatch.setenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", names)
    get_settings.cache_clear()


def _service_operator() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        name="Delete names",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=OPERATOR_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("names", [_OTHER_NAMES, "", " , "], ids=["other", "empty", "blank"])
def test_switch_on_keeps_every_built_in_delete_name(
    monkeypatch: pytest.MonkeyPatch, names: str
) -> None:
    _env(monkeypatch, switch_on=True, names=names)

    patterns = get_settings().service_grant_delete_shaped_patterns

    assert set(DELETE_SHAPED_OP_ID_PATTERNS) <= set(patterns)
    assert patterns[: len(_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS)] == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


def test_switch_on_adds_the_listed_names_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _env(monkeypatch, switch_on=True, names=f"{_OTHER_NAMES}, *.delete, acme.custom.nuke")

    assert get_settings().service_grant_delete_shaped_patterns == (
        *_DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS,
        "acme.custom.nuke",
        "POST:/acme/*/wipe",
    )


def test_switch_off_the_setting_replaces_the_built_in_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, switch_on=False, names=_OTHER_NAMES)

    assert get_settings().service_grant_delete_shaped_patterns == (
        "acme.custom.nuke",
        "POST:/acme/*/wipe",
    )


def test_switch_off_an_empty_setting_keeps_the_built_in_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(monkeypatch, switch_on=False, names="")

    assert get_settings().service_grant_delete_shaped_patterns == (
        _DEFAULT_SERVICE_GRANT_DELETE_SHAPED_PATTERNS
    )


# ---------------------------------------------------------------------------
# Each reader of the setting
# ---------------------------------------------------------------------------

#: ``(switch on, setting, the reader sees the delete sign)``. ``None`` is no
#: setting at all. The last case is the control: with the switch off the
#: setting replaced ``*.delete``, so a reader that ignored the setting fails it.
_CASES = pytest.mark.parametrize(
    ("switch_on", "names", "sees_delete"),
    [
        (True, _OTHER_NAMES, True),
        (True, "", True),
        (True, None, True),
        (False, None, True),
        (False, _OTHER_NAMES, False),
    ],
    ids=["on-other", "on-empty", "on-unset", "off-unset", "off-other"],
)


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


#: The op id of each handler run. A typed handler must be a module-level
#: function, so the runs go to a module-level list that each test clears.
_HANDLER_RUNS: list[str] = []


async def _delete_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _HANDLER_RUNS.append(_DELETE_OP)
    return {"done": True}


async def _reboot_handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    _HANDLER_RUNS.append(_REBOOT_OP)
    return {"done": True}


@pytest.fixture
def handler_runs() -> Iterator[list[str]]:
    """The op id of each handler run in this test."""
    _HANDLER_RUNS.clear()
    yield _HANDLER_RUNS
    _HANDLER_RUNS.clear()


@pytest_asyncio.fixture
async def _registered_ops(embedding: AsyncMock, handler_runs: list[str]) -> AsyncIterator[None]:
    """The two ``caution`` operations, and the tenant of the service login.

    At the end the handler cache forgets the two handlers, so no later test
    file in the same process finds them there.
    """
    handlers = (_delete_handler, _reboot_handler)
    for op_id, handler in zip((_DELETE_OP, _REBOOT_OP), handlers, strict=True):
        await register_typed_operation(
            **_FIX,
            op_id=op_id,
            handler=handler,
            summary="Fixture op.",
            description="Fixture op.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="vm",
            when_to_use="Fixture group.",
            safety_level="caution",
            embedding_service=embedding,
        )
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=OPERATOR_TENANT_ID, slug="dn-tenant", name="Delete Names Tenant"))
    try:
        yield
    finally:
        for handler in handlers:
            _HANDLER_CACHE.pop(f"{handler.__module__}.{handler.__qualname__}", None)


async def _delete_descriptor() -> EndpointDescriptor:
    descriptor = await lookup_descriptor(tenant_id=OPERATOR_TENANT_ID, op_id=_DELETE_OP, **_FIX)
    assert descriptor is not None
    return descriptor


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_never_covered_reason_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    """The one rule of the standing grant and the run approval at call time."""
    descriptor = await _delete_descriptor()
    _env(monkeypatch, switch_on=switch_on, names=names)

    never = never_covered_reason(descriptor)

    assert (never is not None and never[0] == "delete_shaped") is sees_delete, never


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_fixed_rule_for_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    descriptor = await _delete_descriptor()
    _env(monkeypatch, switch_on=switch_on, names=names)

    rule = fixed_rule_for(descriptor, _service_operator())

    assert rule == (DELETE_WAITS if sees_delete else None)


@_CASES
def test_a_step_of_a_multi_step_operation_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    _env(monkeypatch, switch_on=switch_on, names=names)

    step = classify_subop_grantability(_DELETE_OP)

    assert step.grantable is not sees_delete, step


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_grant_create_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    _env(monkeypatch, switch_on=switch_on, names=names)
    payload = ServiceGrantCreate(
        principal_sub=_SERVICE_SUB,
        op_id=_DELETE_OP,
        connector_id=_CONNECTOR,
        target_id=None,
        reason="fixture: standing grant",
        expires_at=None,
    )
    create = ServicePrincipalGrantService().create(OPERATOR_TENANT_ID, "fixture-admin", payload)

    if sees_delete:
        with pytest.raises(GrantValidationError, match="delete-shaped"):
            await create
    else:
        assert (await create).op_id == _DELETE_OP


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_run_approval_list_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    _env(monkeypatch, switch_on=switch_on, names=names)
    payload = RunApprovalCreate(
        work_ref="run-delete-names",
        entries=[RunApprovalEntry(connector_id=_CONNECTOR, op_id=_DELETE_OP)],
        reason="fixture run",
    )
    request = RunApprovalService().request(_service_operator(), payload)

    if sees_delete:
        with pytest.raises(RunApprovalValidationError, match="delete-shaped"):
            await request
    else:
        assert (await request).entries[0].op_id == _DELETE_OP


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_shadow_marks_see_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    descriptor = await _delete_descriptor()
    _env(monkeypatch, switch_on=switch_on, names=names)

    asked = asked_of(descriptor, connector_id=_CONNECTOR, target=None)

    assert ("delete_shaped" in asked.op_class) is sees_delete, asked.op_class


@_CASES
@pytest.mark.usefixtures("_registered_ops")
async def test_flight_recorder_sees_the_delete_name(
    monkeypatch: pytest.MonkeyPatch, switch_on: bool, names: str | None, sees_delete: bool
) -> None:
    """Both reads: the call's context, and the default of ``classify_body_exclusion``."""
    descriptor = await _delete_descriptor()
    _env(monkeypatch, switch_on=switch_on, names=names)
    context = _op_context_from(descriptor, _CONNECTOR)

    for patterns in (context.delete_shaped_patterns, None):
        exclusion = classify_body_exclusion(_DELETE_OP, delete_shaped_patterns=patterns)
        assert exclusion.excluded is sees_delete, (patterns, exclusion)
        if sees_delete:
            assert exclusion.family == "destructive"


# ---------------------------------------------------------------------------
# A whole call: a service login over MCP, under a standing grant
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def _grants(_registered_ops: None) -> AsyncIterator[dict[str, uuid.UUID]]:
    """A standing grant of the service login on each of the two operations.

    The rows are stored directly: the grant route refuses a delete, so the
    row of the delete stands for a grant made before the install changed
    its list.
    """
    grants = {op_id: uuid.uuid4() for op_id in (_DELETE_OP, _REBOOT_OP)}
    async with get_sessionmaker()() as session, session.begin():
        for op_id, grant_id in grants.items():
            session.add(
                ServicePrincipalGrant(
                    id=grant_id,
                    tenant_id=OPERATOR_TENANT_ID,
                    principal_sub=_SERVICE_SUB,
                    op_id=op_id,
                    connector_id=_CONNECTOR,
                    target_id=None,
                    reason="fixture: standing grant",
                    created_by_sub="fixture-admin",
                )
            )
    yield grants


@pytest.fixture
def _service_client() -> Iterator[TestClient]:
    """The app, started with a service login.

    The start keeps a copy of the typed operation rows for the process, and
    the copy holds the made-up family of this file. At the end the copy is
    dropped, so a later test file in the same process never sees the family.
    """

    async def _fake_verify() -> Operator:
        return _service_operator()

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)
        clear_typed_op_snapshot()


def _call(client: TestClient, op_id: str) -> dict[str, Any]:
    body = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "call_operation",
                "arguments": {"connector_id": _CONNECTOR, "op_id": op_id, "params": {}},
            },
        },
    ).json()
    content: dict[str, Any] = body["result"]["structuredContent"]
    return content


async def _waiting_ops() -> list[str]:
    async with get_sessionmaker()() as session:
        return list(await session.scalars(select(ApprovalRequest.op_id)))


async def _audit_rows(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AuditLog).order_by(AuditLog.occurred_at))
        return [row for row in rows if row.payload.get("op_id") == op_id]


async def _grant_use_rows(grant_id: uuid.UUID) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = await session.scalars(select(AuditLog))
        return [row for row in rows if row.payload.get("grant_id") == str(grant_id)]


async def test_switch_on_a_built_in_delete_name_waits_despite_a_grant(
    monkeypatch: pytest.MonkeyPatch,
    _grants: dict[str, uuid.UUID],
    _service_client: TestClient,
    handler_runs: list[str],
) -> None:
    op_id = _DELETE_OP
    _env(monkeypatch, switch_on=True, names=_OTHER_NAMES)

    content = _call(_service_client, op_id)

    assert content["status"] == "awaiting_approval", content
    assert await _waiting_ops() == [op_id]
    assert handler_runs == []
    assert await _grant_use_rows(_grants[op_id]) == []
    rows = await _audit_rows(op_id)
    assert [(row.path, row.status_code) for row in rows] == [("approval.request", 202)]
    assert rows[0].payload["principal_sub"] == _SERVICE_SUB


async def test_switch_on_an_op_without_a_delete_sign_runs_under_the_grant(
    monkeypatch: pytest.MonkeyPatch,
    _grants: dict[str, uuid.UUID],
    _service_client: TestClient,
    handler_runs: list[str],
) -> None:
    """The control case: the same call of an op with no delete sign runs at once."""
    op_id = _REBOOT_OP
    _env(monkeypatch, switch_on=True, names=_OTHER_NAMES)

    content = _call(_service_client, op_id)

    assert content["status"] == "ok", content
    assert await _waiting_ops() == []
    assert handler_runs == [op_id]
    assert len(await _grant_use_rows(_grants[op_id])) == 1


async def test_switch_off_the_same_grant_lets_the_call_run(
    monkeypatch: pytest.MonkeyPatch,
    _grants: dict[str, uuid.UUID],
    _service_client: TestClient,
    handler_runs: list[str],
) -> None:
    """The control case: with the switch off the setting replaced ``*.delete``."""
    _env(monkeypatch, switch_on=False, names=_OTHER_NAMES)

    content = _call(_service_client, _DELETE_OP)

    assert content["status"] == "ok", content
    assert await _waiting_ops() == []
    assert handler_runs == [_DELETE_OP]
    assert len(await _grant_use_rows(_grants[_DELETE_OP])) == 1
