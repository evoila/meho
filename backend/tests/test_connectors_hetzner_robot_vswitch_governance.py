# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Every Hetzner Robot vSwitch write waits for a human approval (#3973).

The connector's safety floor (``hetzner_robot/ingest_safety.py``) raises the
four vSwitch writes after every ingest: membership add and remove and the
rename / VLAN change to ``dangerous``, the cancel to ``destructive``, all
with ``requires_approval``. These tests drive the real ingest, curation,
dispatch and approval code against a respx-mocked Robot and prove:

* the levels hold after a first and a second ingest, even after someone
  lowered the approval flag by hand in between;
* curation switches on only the two membership ops (with their agent
  guidance); the rename and cancel ops stay off;
* a human waits; the requester cannot approve, not even with
  ``APPROVAL_ALLOW_SELF_APPROVAL=true``; an agent without permission is
  refused; a service login waits; a standing grant for either membership op
  is refused;
* a parked change approved and resumed twice sends exactly one request to
  Robot, and the second resume returns ``already_resumed``;
* ``preview_operation`` says the change needs approval and shows the
  vSwitch id and the server list;
* the cancel op, even if someone switches it on, cannot wait for approval
  without a blast-radius statement, so it is refused before Robot is called.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
import respx
from sqlalchemy import func, select, update

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.hetzner_robot import (
    ROBOT_CONNECTOR_ID,
    ROBOT_CORE_GROUPS,
    ROBOT_CORE_OPS,
    ROBOT_IMPL_ID,
    ROBOT_PRODUCT,
    ROBOT_VERSION,
    apply_robot_core_curation,
    classify_robot_op,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor, OperationGroup, Target
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations.approval_queue import (
    ApprovalRequestAlreadyDecidedError,
    SelfApprovalForbiddenError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.ingest import ReviewService
from meho_backplane.operations.meta_tools import preview_operation
from meho_backplane.operations.service_grant_schemas import ServiceGrantCreate
from meho_backplane.operations.service_grants import (
    GrantValidationError,
    ServicePrincipalGrantService,
)
from meho_backplane.settings import get_settings

from ._robot_vswitch_fixtures import (
    ADD_OP,
    BASE_URL,
    CANCEL_OP,
    REMOVE_OP,
    RENAME_OP,
    TARGET_NAME,
    TENANT,
    VSWITCH_ID,
    enable_ops,
    ingest_shipped_spec,
    install_connector,
    load_rows,
    make_embedding_service,
    make_operator,
    membership_params,
    register_robot_connector,
    seed_target,
)

_MEMBERSHIP_PATH = f"/vswitch/{VSWITCH_ID}/server"
_EXPECTED_FLOOR = {
    ADD_OP: "dangerous",
    REMOVE_OP: "dangerous",
    RENAME_OP: "dangerous",
    CANCEL_OP: "destructive",
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("APPROVAL_ALLOW_SELF_APPROVAL", raising=False)
    get_settings.cache_clear()
    register_robot_connector()
    yield
    reset_dispatcher_caches()
    get_settings.cache_clear()


async def _setup(*op_ids: str) -> Target:
    await ingest_shipped_spec(make_embedding_service())
    await enable_ops(*op_ids)
    target = await seed_target()
    install_connector()
    return target


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _park(target: Target, requester: Operator, op_id: str = ADD_OP) -> UUID:
    result = await dispatch(
        operator=requester,
        connector_id=ROBOT_CONNECTOR_ID,
        op_id=op_id,
        target=target,
        params=membership_params(321, "1.2.3.4"),
    )
    assert result.status == "awaiting_approval", result
    return UUID(result.extras["approval_request_id"])


# ---------------------------------------------------------------------------
# The floor holds after every ingest
# ---------------------------------------------------------------------------


async def test_floor_holds_after_ingest_and_reingest() -> None:
    embedding = make_embedding_service()
    await ingest_shipped_spec(embedding)
    first = await load_rows(_EXPECTED_FLOOR)
    assert {op: (row.safety_level, row.requires_approval) for op, row in first.items()} == {
        op: (level, True) for op, level in _EXPECTED_FLOOR.items()
    }

    # Someone lowers the flag by hand (``meho connector edit-op``) ...
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.op_id == ADD_OP)
            .values(requires_approval=False, safety_level="caution")
        )
        await session.commit()

    # ... and the next ingest puts the floor back.
    await ingest_shipped_spec(embedding)
    second = await load_rows(_EXPECTED_FLOOR)
    assert {op: (row.safety_level, row.requires_approval) for op, row in second.items()} == {
        op: (level, True) for op, level in _EXPECTED_FLOOR.items()
    }
    assert all(row.is_enabled is False for row in second.values())


# ---------------------------------------------------------------------------
# Curation: membership on, rename and cancel off
# ---------------------------------------------------------------------------


async def _group_ingested_rows() -> None:
    """Group the ingested rows the way the grouping pass would.

    Every ``/vswitch`` op — the rename and cancel included — lands in
    ``robot-networking``; the rest follow :func:`classify_robot_op`. The
    shipped spec lacks four curated reads (``/ip``, ``/subnet``,
    ``/failover``, ``/key`` — a known gap, out of scope here), so they are
    added as plain rows first, as a fuller spec would provide them.
    """
    async with get_sessionmaker()() as session:
        present = set((await session.execute(select(EndpointDescriptor.op_id))).scalars())
        for op in ROBOT_CORE_OPS:
            if op.op_id in present:
                continue
            method, path = op.op_id.split(":", 1)
            session.add(
                EndpointDescriptor(
                    tenant_id=None,
                    product=ROBOT_PRODUCT,
                    version=ROBOT_VERSION,
                    impl_id=ROBOT_IMPL_ID,
                    op_id=op.op_id,
                    source_kind="ingested",
                    method=method,
                    path=path,
                    handler_ref=None,
                    summary=f"Read {path}.",
                    description=f"Read {path}.",
                    parameter_schema={"type": "object", "properties": {}},
                    response_schema={"type": "object"},
                    safety_level="safe",
                    requires_approval=False,
                    is_enabled=False,
                    tags=[],
                )
            )
        await session.commit()
    async with get_sessionmaker()() as session:
        group_ids: dict[str, UUID] = {}
        for group in ROBOT_CORE_GROUPS:
            row = OperationGroup(
                tenant_id=None,
                product=ROBOT_PRODUCT,
                version=ROBOT_VERSION,
                impl_id=ROBOT_IMPL_ID,
                group_key=group.group_key,
                name=f"LLM name for {group.group_key}",
                when_to_use="LLM hint.",
                review_status="staged",
            )
            session.add(row)
            await session.flush()
            group_ids[group.group_key] = row.id
        descriptors = (await session.execute(select(EndpointDescriptor))).scalars().all()
        for descriptor in descriptors:
            path = descriptor.op_id.split(":", 1)[1]
            key = (
                "robot-networking"
                if path.startswith("/vswitch")
                else classify_robot_op(descriptor.op_id)
            )
            if key in group_ids:
                descriptor.group_id = group_ids[key]
        await session.commit()


async def test_curation_switches_on_membership_and_keeps_rename_and_cancel_off() -> None:
    await ingest_shipped_spec(make_embedding_service())
    await _group_ingested_rows()
    admin = Operator(
        sub="robot-curator",
        name=None,
        email=None,
        raw_jwt="<robot-curator-raw-jwt>",
        tenant_id=TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        platform_admin=True,  # the shipped spec is ingested as a built-in connector
    )
    await apply_robot_core_curation(ReviewService(operator=admin), tenant_id=None)

    rows = await load_rows(_EXPECTED_FLOOR)
    guidance = {op.op_id: op.llm_instructions for op in ROBOT_CORE_OPS}
    for op_id in (ADD_OP, REMOVE_OP):
        assert rows[op_id].is_enabled is True
        assert rows[op_id].llm_instructions == guidance[op_id]
    for op_id in (RENAME_OP, CANCEL_OP):
        assert rows[op_id].is_enabled is False
    assert rows[CANCEL_OP].safety_level == "destructive"


# ---------------------------------------------------------------------------
# Who waits, who is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", [ADD_OP, REMOVE_OP])
async def test_human_login_waits_and_robot_is_not_called(op_id: str) -> None:
    target = await _setup(op_id)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route(path=_MEMBERSHIP_PATH).respond(200, content=b"")
        await _park(target, make_operator(), op_id)
    assert route.call_count == 0
    assert await _approval_count() == 1


async def test_requester_cannot_approve_even_with_break_glass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = await _setup(ADD_OP)
    requester = make_operator("solo-operator")
    request_id = await _park(target, requester)

    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true")
    get_settings.cache_clear()
    async with get_sessionmaker()() as session:
        with pytest.raises(SelfApprovalForbiddenError):
            await approve_request(session, request_id, operator=requester, params=None)


async def test_agent_without_permission_is_refused() -> None:
    target = await _setup(ADD_OP)
    result = await dispatch(
        operator=make_operator("robot-agent", principal_kind=PrincipalKind.AGENT),
        connector_id=ROBOT_CONNECTOR_ID,
        op_id=ADD_OP,
        target=target,
        params=membership_params(321),
    )
    assert result.status == "denied", result
    assert await _approval_count() == 0


async def test_service_login_waits_for_approval() -> None:
    target = await _setup(ADD_OP)
    await _park(target, make_operator("robot-service", principal_kind=PrincipalKind.SERVICE))
    assert await _approval_count() == 1


@pytest.mark.parametrize("op_id", [ADD_OP, REMOVE_OP])
async def test_standing_grant_for_a_membership_op_is_refused(op_id: str) -> None:
    await _setup(op_id)
    payload = ServiceGrantCreate(
        principal_sub="robot-service",
        op_id=op_id,
        connector_id=ROBOT_CONNECTOR_ID,
        target_id=None,
        reason="unattended vSwitch changes",
        expires_at=None,
    )
    with pytest.raises(GrantValidationError, match="never grantable"):
        await ServicePrincipalGrantService().create(TENANT, "robot-admin", payload)


# ---------------------------------------------------------------------------
# Approve once, resume twice: exactly one request reaches Robot
# ---------------------------------------------------------------------------


async def test_a_parked_change_reaches_robot_exactly_once() -> None:
    target = await _setup(ADD_OP)
    request_id = await _park(target, make_operator("robot-requester"))
    approver = make_operator("robot-approver")

    async with get_sessionmaker()() as session:
        await approve_request(session, request_id, operator=approver, params=None)
        await session.commit()
    async with get_sessionmaker()() as session:
        with pytest.raises(ApprovalRequestAlreadyDecidedError):
            await approve_request(session, request_id, operator=approver, params=None)

    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.post(_MEMBERSHIP_PATH).respond(200, content=b"")
        results = []
        for _ in range(2):
            async with get_sessionmaker()() as session:
                row = await session.get(ApprovalRequest, request_id)
            assert row is not None
            results.append(
                await resume_dispatch_after_approval(operator=approver, request=row, params=None)
            )

    assert [r.status for r in results] == ["ok", "already_resumed"]
    assert results[0].result == {}
    assert route.call_count == 1


# ---------------------------------------------------------------------------
# preview_operation shows the approval need, the vSwitch and the servers
# ---------------------------------------------------------------------------


async def test_preview_shows_approval_vswitch_and_servers() -> None:
    await _setup(ADD_OP)
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route(path=_MEMBERSHIP_PATH).respond(200, content=b"")
        preview = await preview_operation(
            make_operator(),
            {
                "connector_id": ROBOT_CONNECTOR_ID,
                "op_id": ADD_OP,
                "target": TARGET_NAME,
                "params": membership_params(321, "1.2.3.4"),
            },
        )
    assert preview["status"] == "ok", preview
    assert preview["requires_approval"] is True
    assert preview["safety_level"] == "dangerous"
    assert preview["method"] == "POST"
    assert preview["resolved_path"] == _MEMBERSHIP_PATH
    assert preview["redacted_body"] == {"server": [321, "1.2.3.4"]}
    assert route.call_count == 0


# ---------------------------------------------------------------------------
# The cancel op fails closed even if someone switches it on
# ---------------------------------------------------------------------------


async def test_cancel_op_is_refused_without_a_blast_radius_statement() -> None:
    target = await _setup(CANCEL_OP)
    params = {"vswitch-id": VSWITCH_ID, "body": {"cancellation_date": "now"}}
    preview = await preview_operation(
        make_operator(),
        {
            "connector_id": ROBOT_CONNECTOR_ID,
            "op_id": CANCEL_OP,
            "target": TARGET_NAME,
            "params": params,
        },
    )
    assert preview["safety_level"] == "destructive"
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        route = mock.route(path=f"/vswitch/{VSWITCH_ID}").respond(200, content=b"")
        result = await dispatch(
            operator=make_operator(),
            connector_id=ROBOT_CONNECTOR_ID,
            op_id=CANCEL_OP,
            target=target,
            params=params,
            preview_hash=preview["preview_hash"],
        )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "blast_radius_required"
    assert route.call_count == 0
    assert await _approval_count() == 0
