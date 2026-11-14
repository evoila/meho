# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The stricter of install and tenant counts, for the risk level and for "always wait".

* :func:`~meho_backplane.policy.classification.classify`: an install risk
  level is a floor for every tenant; a tenant can raise it, not lower it.
  A tenant's own "always wait" holds under a more specific install entry
  with ``always_wait: false``.
* The policy file: a tenant entry with a lower risk level than a matching
  install entry is refused, and the text names the install entry. The
  same holds on the REST import route.
* The upgrade case: a stored tenant entry with a lower level no longer
  lowers it, and the export marks only that part "Has no effect".
* An install-wide wait with a standing grant and with a run approval of
  a tenant: the call still waits, each time.
* The single edit and the admin MCP tool: a tenant wait under an install
  ``false`` is stored and holds; a category edit is not refused.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import register_policy_apply_operation
from meho_backplane.connectors.policy.classify import register_policy_classify_operation
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EndpointDescriptor,
    PermissionVerdict,
    RunApproval,
    ServicePrincipalGrant,
    TenantPolicy,
)
from meho_backplane.main import app
from meho_backplane.mcp.registry import get_tool
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.operations._lookup import lookup_descriptor
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.composite import enforce_subop_policy
from meho_backplane.operations.op_policy import effective_policy_map
from meho_backplane.operations.typed_register import register_typed_operation
from meho_backplane.policy.bulk import preview_classification_edit
from meho_backplane.policy.classification import (
    ClassificationEntry,
    OpCoordinates,
    Taxonomy,
    classify,
)
from meho_backplane.policy.service import build_plan, export_policy
from meho_backplane.policy.store import ClassificationEdit, set_classification
from meho_backplane.policy.validate import KnownOp, check_document
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import (
    DEFAULT_TENANT_ID,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)
from ._vault_fakes import install_fake_vault

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006463")
_FIX = {"product": "irfix", "version": "1.x", "impl_id": "irfix"}
_CONNECTOR = "irfix-1.x"
_STOP = "irfix.vm.stop"
_LIST = "irfix.vm.list"
_COORDS = OpCoordinates(_CONNECTOR, "vm", _STOP)
_TAX = Taxonomy(categories=("compute", "other"), default="other")
_SERVICE_SUB = "svc:install-floor"
_RUN = "run-install-floor"

_INSTALL_RISK = {"classifications": [{"match": {"op": _STOP}, "risk": "destructive"}]}
_TENANT_LOWER = {"classifications": [{"match": {"op": "irfix.vm.*"}, "risk": "caution"}]}
_RISK_REFUSAL = (
    "sets `risk: caution` on `irfix.vm.stop`, but `install:classifications[0]` sets it to "
    "`destructive` for the whole install. A tenant can raise a risk level, not lower it. "
    "Remove `risk: caution` from the tenant entry, or set `destructive` or higher; its "
    "other fields can stay."
)
_INSTALL_WAIT = {"classifications": [{"match": {"op": _STOP}, "always_wait": True}]}
_TENANT_UNDO = {"classifications": [{"match": {"op": "irfix.vm.*"}, "always_wait": False}]}
#: The install makes the group wait, and then sets one operation back.
_INSTALL_FALSE = {
    "classifications": [
        {"match": {"group": "vm"}, "always_wait": True},
        {"match": {"op": _STOP}, "always_wait": False},
    ]
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _install(ref: str, **fields: Any) -> ClassificationEntry:
    return ClassificationEntry(ref=ref, scope="install", **fields)


def _tenant(ref: str, **fields: Any) -> ClassificationEntry:
    return ClassificationEntry(ref=ref, scope="tenant", **fields)


# ---------------------------------------------------------------------------
# classify()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant_entry",
    [
        pytest.param(_tenant("t", op=_STOP, risk="safe"), id="same-rank"),
        pytest.param(_tenant("t", op="irfix.vm.st*", risk="caution"), id="pattern"),
        pytest.param(_tenant("t", connector=_CONNECTOR, risk="dangerous"), id="connector"),
    ],
)
def test_an_install_risk_level_is_a_floor(tenant_entry: ClassificationEntry) -> None:
    install = _install("i", connector="irfix-*", risk="destructive")
    assert classify(_COORDS, [install, tenant_entry], _TAX).risk == "destructive"


def test_a_tenant_can_raise_the_install_level() -> None:
    entries = [
        _install("i", op=_STOP, risk="caution"),
        _tenant("t", connector=_CONNECTOR, risk="dangerous"),
    ]
    assert classify(_COORDS, entries, _TAX).risk == "dangerous"


def test_the_installs_own_more_specific_level_decides_the_floor() -> None:
    entries = [
        _install("broad", connector="irfix-*", risk="destructive"),
        _install("narrow", op=_STOP, risk="caution"),
        _tenant("t", op=_STOP, risk="safe"),
    ]
    assert classify(_COORDS, entries, _TAX).risk == "caution"


def test_a_tenant_can_lower_its_own_level() -> None:
    entries = [
        _tenant("broad", connector=_CONNECTOR, risk="destructive"),
        _tenant("narrow", op=_STOP, risk="caution"),
    ]
    assert classify(_COORDS, entries, _TAX).risk == "caution"


def test_a_tenant_wait_holds_under_a_more_specific_install_false() -> None:
    entries = [
        _install("i", op=_STOP, always_wait=False),
        _tenant("t", connector=_CONNECTOR, always_wait=True),
    ]
    assert classify(_COORDS, entries, _TAX).always_wait is True


def test_a_tenant_can_end_its_own_wait() -> None:
    entries = [
        _tenant("broad", connector=_CONNECTOR, always_wait=True),
        _tenant("narrow", op=_STOP, always_wait=False),
    ]
    assert classify(_COORDS, entries, _TAX).always_wait is False


# ---------------------------------------------------------------------------
# The policy file
# ---------------------------------------------------------------------------


def _op(op_id: str) -> KnownOp:
    return KnownOp(
        coords=OpCoordinates(_CONNECTOR, "vm", op_id),
        safety_level="safe",
        shows_secret=False,
        caller_address=False,
    )


_OPS = [_op(_LIST), _op(_STOP)]


def test_a_tenant_file_with_a_lower_level_is_refused() -> None:
    mistakes = check_document(
        _TENANT_LOWER, scope="tenant", install_document=_INSTALL_RISK, ops=_OPS
    )
    assert mistakes == [f"`classifications[0]` {_RISK_REFUSAL}"]


def test_an_entry_that_lowers_the_level_and_ends_the_wait_gets_two_mistakes() -> None:
    install = {
        "classifications": [{"match": {"op": _STOP}, "risk": "destructive", "always_wait": True}]
    }
    tenant = {
        "classifications": [
            {"match": {"op": "irfix.vm.*"}, "risk": "caution", "always_wait": False}
        ]
    }
    mistakes = check_document(tenant, scope="tenant", install_document=install, ops=_OPS)
    assert len(mistakes) == 2
    assert "always_wait: false" in mistakes[0]
    assert "risk: caution" in mistakes[1]


@pytest.mark.parametrize(
    "document",
    [
        pytest.param(
            {"classifications": [{"match": {"op": _STOP}, "risk": "destructive"}]}, id="same-level"
        ),
        pytest.param(
            {"classifications": [{"match": {"op": _LIST}, "risk": "caution"}]},
            id="where-the-install-sets-no-level",
        ),
    ],
)
def test_a_tenant_file_with_the_same_level_or_another_operation_is_fine(
    document: dict[str, Any],
) -> None:
    assert check_document(document, scope="tenant", install_document=_INSTALL_RISK, ops=_OPS) == []


def test_the_installs_own_lower_level_is_the_floor_for_the_file() -> None:
    install = {
        "classifications": [
            {"match": {"connector": "irfix-*"}, "risk": "destructive"},
            {"match": {"op": _STOP}, "risk": "caution"},
        ]
    }
    tenant = {"classifications": [{"match": {"op": _STOP}, "risk": "caution"}]}
    assert check_document(tenant, scope="tenant", install_document=install, ops=[_op(_STOP)]) == []


def test_the_install_file_itself_is_not_checked_against_itself() -> None:
    assert check_document(_INSTALL_RISK, scope="install", install_document=None, ops=_OPS) == []


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.25] * 384
    service.encode.return_value = [[0.25] * 384]
    service.dimension = 384
    return service


async def _handler(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    return {}


@pytest.fixture
async def ops(embedding: AsyncMock) -> None:
    """Two ``caution`` operations: a service call waits unless a grant clears it."""
    for op_id in (_LIST, _STOP):
        await register_typed_operation(
            **_FIX,
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="vm",
            when_to_use="Fixture group.",
            safety_level="caution",
            embedding_service=embedding,
        )
    await register_policy_classify_operation(embedding_service=embedding)


async def _store(tenant_id: uuid.UUID | None, document: dict[str, Any]) -> None:
    async with get_sessionmaker()() as session:
        session.add(TenantPolicy(tenant_id=tenant_id, document=document, set_by_sub="fixture"))
        await session.commit()


async def _policies(tenant_id: uuid.UUID) -> dict[str, tuple[str, bool, bool]]:
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "irfix")
                )
            ).scalars()
        )
        policies = await effective_policy_map(session, tenant_id=tenant_id, rows=rows)
    return {
        k.op_id: (p.safety_level, p.requires_approval, p.always_wait) for k, p in policies.items()
    }


def _admin(tenant_id: uuid.UUID = _TENANT) -> Operator:
    return Operator(
        sub="admin-a",
        tenant_id=tenant_id,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
        raw_jwt="header.payload.signature",
    )


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_an_install_level_holds_in_two_tenants_and_after_the_upgrade() -> None:
    await _store(None, _INSTALL_RISK)
    # Stored before the upgrade: this entry used to lower the level.
    await _store(_TENANT, _TENANT_LOWER)
    for tenant_id in (_TENANT, _OTHER_TENANT):
        policies = await _policies(tenant_id)
        assert policies[_STOP] == ("destructive", True, False), tenant_id
        assert policies[_LIST] == ("caution", False, False), tenant_id


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_stored_tenant_wait_holds_under_an_install_false() -> None:
    await _store(None, _INSTALL_FALSE)
    await _store(_TENANT, {"classifications": [{"match": {"group": "vm"}, "always_wait": True}]})
    assert (await _policies(_TENANT))[_STOP] == ("caution", True, True)
    assert (await _policies(_OTHER_TENANT))[_STOP] == ("caution", False, False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_export_marks_only_the_part_that_has_no_effect() -> None:
    await _store(None, _INSTALL_RISK)
    await _store(
        _TENANT,
        {
            "classifications": [
                {"match": {"op": "irfix.vm.*"}, "category": "compute", "risk": "caution"},
            ]
        },
    )
    async with get_sessionmaker()() as session:
        text = await export_policy(session, scope="tenant", tenant_id=_TENANT)
        plan = await build_plan(session, scope="tenant", tenant_id=_TENANT, content=text.encode())
    assert text == (
        "policy: v1\n"
        "classifications:\n"
        f"# Has no effect: `classifications[0]` {_RISK_REFUSAL}\n"
        "- match:\n"
        "    op: irfix.vm.*\n"
        "  category: compute\n"
        "  risk: caution\n"
    )
    assert plan.mistakes == [f"`classifications[0]` {_RISK_REFUSAL}"]


# ---------------------------------------------------------------------------
# The REST import route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_rest_import_refuses_a_lower_level(
    embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_vault(monkeypatch)
    await register_policy_apply_operation(embedding_service=embedding)
    for op_id in (_LIST, _STOP):
        await register_typed_operation(
            **_FIX,
            op_id=op_id,
            handler=_handler,
            summary="Fixture operation.",
            description="Fixture operation.",
            parameter_schema={"type": "object", "properties": {}},
            group_key="vm",
            when_to_use="Fixture group.",
            embedding_service=embedding,
        )
    await _store(None, _INSTALL_RISK)
    key = make_rsa_keypair("kid-install-risk-floor")
    token = mint_token(
        key, sub="admin-a", tenant_id=str(_TENANT), tenant_role=TenantRole.TENANT_ADMIN.value
    )
    content = "policy: v1\nclassifications:\n  - {match: {op: irfix.vm.*}, risk: caution}\n"
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        resp = TestClient(app).post(
            "/api/v1/policy/import",
            content=content.encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/yaml"},
        )
    assert resp.status_code == 422, resp.text
    assert resp.json()["mistakes"] == [f"`classifications[0]` {_RISK_REFUSAL}"]
    async with get_sessionmaker()() as session:
        assert list((await session.execute(select(ApprovalRequest))).scalars()) == []


# ---------------------------------------------------------------------------
# Standing grant and run approval
# ---------------------------------------------------------------------------


def _service() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _seed_grant_and_run_approval() -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                op_id=_STOP,
                connector_id=_CONNECTOR,
                target_id=None,
                reason="fixture: made before the install policy",
                created_by_sub="fixture-admin",
            )
        )
        session.add(
            RunApproval(
                id=uuid.uuid4(),
                tenant_id=_TENANT,
                principal_sub=_SERVICE_SUB,
                work_ref=_RUN,
                entries=[{"connector_id": _CONNECTOR, "op_id": _STOP}],
                reason="fixture: approved before the install policy",
                requested_at=now,
                expires_at=now + timedelta(hours=1),
                approved_at=now,
                approved_by_sub="person-approver",
            )
        )
        await session.commit()


async def _gate() -> PermissionVerdict:
    descriptor = await lookup_descriptor(tenant_id=_TENANT, op_id=_STOP, **_FIX)
    assert descriptor is not None
    verdict, _reason = await policy_gate(
        operator=_service(), descriptor=descriptor, target=None, connector_id=_CONNECTOR
    )
    return verdict


async def _step_status() -> str | None:
    token = work_ref_var.set(_RUN)
    try:
        result = await enforce_subop_policy(
            operator=_service(),
            connector_id=_CONNECTOR,
            op_id=_STOP,
            safety_level="caution",
            requires_approval=False,
            target=None,
            params={"vm": "fixture"},
        )
    finally:
        work_ref_var.reset(token)
    return None if result is None else result.status


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_grant_and_run_approval_clear_the_call_without_an_install_policy() -> None:
    """The control case: the same grant and run approval, and no install entry."""
    await _store(_TENANT, _TENANT_UNDO)
    await _seed_grant_and_run_approval()
    assert await _gate() is PermissionVerdict.AUTO_EXECUTE
    assert await _step_status() is None


@pytest.mark.parametrize(
    ("install", "tenant", "step_status"),
    [
        pytest.param(_INSTALL_WAIT, _TENANT_UNDO, "awaiting_approval", id="install-wait"),
        # A destructive step is refused under any grant: it never runs.
        pytest.param(_INSTALL_RISK, _TENANT_LOWER, "denied", id="install-destructive"),
    ],
)
@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_an_install_floor_waits_each_time_despite_a_grant_and_a_run_approval(
    install: dict[str, Any], tenant: dict[str, Any], step_status: str
) -> None:
    await _store(None, install)
    await _store(_TENANT, tenant)
    await _seed_grant_and_run_approval()
    for _ in range(2):
        assert await _gate() is PermissionVerdict.NEEDS_APPROVAL
        assert await _step_status() == step_status


# ---------------------------------------------------------------------------
# The single edit and the admin MCP tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_single_edit_adds_a_wait_under_an_install_false() -> None:
    await _store(None, _INSTALL_FALSE)
    edit = ClassificationEdit(match={"group": "vm"}, category=None, labels=None, always_wait=True)
    preview = await preview_classification_edit(_admin(), edit)
    assert preview.conflict is None
    stop = next(op for op in preview.operations if op.op_id == _STOP)
    assert (stop.before.always_wait, stop.after.always_wait) == (False, True)
    async with get_sessionmaker()() as session, session.begin():
        await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    assert (await _policies(_TENANT))[_STOP] == ("caution", True, True)
    assert (await _policies(_OTHER_TENANT))[_STOP] == ("caution", False, False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_a_category_edit_on_an_old_lower_level_is_not_refused() -> None:
    """Only a part an edit sets is checked; the old level still has no effect."""
    await _store(None, _INSTALL_RISK)
    await _store(_TENANT, _TENANT_LOWER)
    edit = ClassificationEdit(
        match={"op": "irfix.vm.*"}, category="compute", labels=None, always_wait=None
    )
    async with get_sessionmaker()() as session, session.begin():
        await set_classification(session, tenant_id=_TENANT, set_by_sub="a", edit=edit)
    assert (await _policies(_TENANT))[_STOP] == ("destructive", True, False)


@pytest.mark.asyncio
@pytest.mark.usefixtures("ops")
async def test_the_admin_mcp_tool_asks_for_a_category_under_an_install_floor() -> None:
    await _store(
        None, {"classifications": [{**_INSTALL_RISK["classifications"][0], "always_wait": True}]}
    )
    found = get_tool("meho_connector_edit_op")
    assert found is not None
    _, handler = found
    result = await handler(
        _admin(), {"connector_id": _CONNECTOR, "op_id": _STOP, "category": "compute"}
    )
    assert result["status"] == "awaiting_approval", result
    assert result["approval_request_id"]
    assert (await _policies(_TENANT))[_STOP] == ("destructive", True, True)
