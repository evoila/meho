# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``preview_operation`` says whether a live grant runs the call at once (#5492).

The operation under test is a synthetic ``destructive`` delete that the test
declares itself: a small connector, a multi-step op that waits for approval,
a preview builder that states the blast radius, and three listed steps (one
read, one delete, one plain write). It runs on the migrated SQLite engine
with the embedding stub.

* With a delete grant for the operation and a grant for each listed write
  step, the preview says ``auto-execute`` and names the grant; the real call
  with the preview's hash then runs with no approval request.
* Without a grant, the preview says the call waits for approval.
* With a grant for the operation but not for its steps, the preview does
  not read as covered.
* The preview uses no grant: no grant-use row, no auto-approval event.
* With sandbox mode off, a ``destructive`` operation keeps its answer.
* The step check: reads are skipped, a denied step denies, an operation
  with no listed steps is not changed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EventOutbox,
    PermissionVerdict,
    ServicePrincipalGrant,
)
from meho_backplane.operations import _preview_steps, reset_dispatcher_caches
from meho_backplane.operations._preview import PreviewContext, register_preview_builder
from meho_backplane.operations._preview_steps import preview_step_verdict
from meho_backplane.operations.governed_subops import (
    register_governed_subops,
    reset_governed_subop_registry,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.operations.typed_register import register_composite_operation
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import DEFAULT_TENANT_ID
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests._targets_helpers import _insert_target

_TENANT_ID = uuid.UUID(DEFAULT_TENANT_ID)
_SERVICE_SUB = "svc:test-runner"
_PRODUCT = "example"
_VERSION = "1.x"
_IMPL_ID = "example-rest"
_CONNECTOR_ID = f"{_IMPL_ID}-{_VERSION}"
_OP_ID = "example.thing.delete"
_TARGET_NAME = "doomed-thing-host"
_PARAMS: dict[str, Any] = {"thing": "thing-1"}
#: The governed steps the test lists for the op. A step id has the
#: ``METHOD:/path`` shape of a vendor call.
_READ_STEP = "GET:/things/{id}"
_DELETE_STEP = "DELETE:/things/{id}"
_WRITE_STEP = "POST:/things/{id}/tags"
_ARGS: dict[str, Any] = {
    "connector_id": _CONNECTOR_ID,
    "op_id": _OP_ID,
    "target": _TARGET_NAME,
    "params": _PARAMS,
}
_GRANT_FIELDS: dict[str, Any] = {
    "tenant_id": _TENANT_ID,
    "principal_sub": _SERVICE_SUB,
    "connector_id": _CONNECTOR_ID,
    "target_product": _PRODUCT,
}


class _ExampleConnector(Connector):
    """A connector the resolver accepts; the op's handler makes no vendor call."""

    product = _PRODUCT
    version = _VERSION
    impl_id = _IMPL_ID

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError


async def _thing_delete(
    operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
) -> dict[str, Any]:
    """The multi-step delete; it records the identifier and makes no vendor call."""
    del operator, target, dispatch_child
    return {"deleted": params["thing"]}


async def _thing_delete_preview(ctx: PreviewContext) -> dict[str, Any]:
    """The blast radius a destructive op must state before it may park or run."""
    thing = str(ctx.params.get("thing"))
    return {
        "blast_radius": {
            "object": {"kind": "thing", "id": thing},
            "children": [],
            "irreversibility": "permanent",
        }
    }


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    reset_governed_subop_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    reset_governed_subop_registry()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _service() -> Operator:
    return Operator(
        sub=_SERVICE_SUB,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.SERVICE,
    )


async def _ready(stub_embedding_service: AsyncMock) -> None:
    """Register the connector, the op, its preview and its steps; seed the target."""
    register_connector_v2(
        product=_PRODUCT, version=_VERSION, impl_id=_IMPL_ID, cls=_ExampleConnector
    )
    await register_composite_operation(
        product=_PRODUCT,
        version=_VERSION,
        impl_id=_IMPL_ID,
        op_id=_OP_ID,
        handler=_thing_delete,
        summary="Delete a thing.",
        description="Delete a thing; a synthetic destructive multi-step operation.",
        parameter_schema={
            "type": "object",
            "properties": {"thing": {"type": "string"}},
            "required": ["thing"],
        },
        when_to_use=None,
        safety_level="destructive",
        requires_approval=True,
        embedding_service=stub_embedding_service,
    )
    register_preview_builder(_OP_ID, _thing_delete_preview)
    register_governed_subops(
        composite_op_id=_OP_ID,
        connector_id=_CONNECTOR_ID,
        sub_op_ids=(_READ_STEP, _DELETE_STEP, _WRITE_STEP),
    )
    await _insert_target(
        tenant_id=_TENANT_ID,
        name=_TARGET_NAME,
        product=_PRODUCT,
        host="203.0.113.5",
        fingerprint={"version": _VERSION},
        preferred_impl_id=_IMPL_ID,
    )


async def _grant_op_and_steps() -> uuid.UUID:
    """Grants for the op and each listed write step; returns the op's grant."""
    op_grant = await seed_delete_grant(op_id=_OP_ID, **_GRANT_FIELDS)
    await seed_delete_grant(op_id=_DELETE_STEP, **_GRANT_FIELDS)
    await seed_grant_row(model=ServicePrincipalGrant, op_id=_WRITE_STEP, **_GRANT_FIELDS)
    return op_grant


async def _pending_count() -> int:
    async with get_sessionmaker()() as session:
        return int(
            (await session.execute(select(func.count()).select_from(ApprovalRequest))).scalar_one()
        )


async def _auto_approved_events() -> list[str]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(EventOutbox))).scalars().all()
    return [row.event_kind for row in rows if "auto_approved" in row.event_kind]


@pytest.mark.asyncio
async def test_live_delete_grant_previews_auto_execute_and_names_it(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _ready(stub_embedding_service)
    op_grant = await _grant_op_and_steps()
    service = _service()

    preview = await preview_operation(service, _ARGS)

    assert preview["status"] == "ok", preview
    assert preview["policy_verdict"] == "auto-execute"
    assert str(op_grant) in preview["policy_reason"]
    assert "preview_hash" in preview["policy_reason"]
    # The preview used no grant.
    assert await grant_use_rows() == []
    assert await _auto_approved_events() == []

    # The real call agrees: it runs at once.
    call = await call_operation(service, {**_ARGS, "preview_hash": preview["preview_hash"]})
    assert call["status"] == "ok", call
    assert await _pending_count() == 0


@pytest.mark.asyncio
async def test_no_grant_previews_that_the_call_waits(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _ready(stub_embedding_service)

    preview = await preview_operation(_service(), _ARGS)

    assert preview["policy_verdict"] == "needs-approval"
    assert "no live delete grant covers this call" in preview["policy_reason"]
    assert "may cover it" not in preview["policy_reason"]
    assert await grant_use_rows() == []


@pytest.mark.parametrize("revoked", [False, True])
@pytest.mark.asyncio
async def test_expired_or_revoked_grant_does_not_read_as_covered(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch, revoked: bool
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _ready(stub_embedding_service)
    if revoked:
        await seed_delete_grant(op_id=_OP_ID, revoked_at=soon(-1), **_GRANT_FIELDS)
    else:
        await seed_delete_grant(op_id=_OP_ID, expires_at=soon(-1), **_GRANT_FIELDS)

    preview = await preview_operation(_service(), _ARGS)

    assert preview["policy_verdict"] == "needs-approval"


@pytest.mark.asyncio
async def test_grant_on_the_operation_alone_does_not_read_as_covered(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_on(monkeypatch)
    await _ready(stub_embedding_service)
    op_grant = await seed_delete_grant(op_id=_OP_ID, **_GRANT_FIELDS)

    preview = await preview_operation(_service(), _ARGS)

    assert preview["policy_verdict"] == "needs-approval"
    reason = preview["policy_reason"]
    assert str(op_grant) in reason
    assert _DELETE_STEP in reason
    assert _WRITE_STEP in reason
    # The read of the thing is not a write step.
    assert _READ_STEP not in reason
    assert await grant_use_rows() == []


@pytest.mark.asyncio
async def test_mode_off_keeps_the_destructive_answer(
    stub_embedding_service: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    turn_sandbox_mode_off(monkeypatch)
    await _ready(stub_embedding_service)
    await _grant_op_and_steps()

    preview = await preview_operation(_service(), _ARGS)

    assert preview["policy_verdict"] == "needs-approval"
    assert "no standing grant can pre-clear it" in preview["policy_reason"]
    assert "sandbox" not in preview["policy_reason"]
    assert await grant_use_rows() == []


# ---------------------------------------------------------------------------
# The step check on its own
# ---------------------------------------------------------------------------

_COMPOSITE = "demo.composite.thing.change"
_STEP_CONNECTOR = "demo-1.0-rest"


@pytest.mark.asyncio
async def test_step_check_without_listed_steps_changes_nothing() -> None:
    assert (
        await preview_step_verdict(
            operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
        )
        is None
    )


@pytest.mark.asyncio
async def test_step_check_skips_reads_and_reports_waiting_and_denied_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_governed_subops(
        composite_op_id=_COMPOSITE,
        connector_id=_STEP_CONNECTOR,
        sub_op_ids=("GET:/things/{id}", "head:/things/{id}", "PATCH:/things/{id}"),
    )
    seen: list[str] = []
    answer = {"verdict": PermissionVerdict.NEEDS_APPROVAL}

    async def fake_gate(**kwargs: Any) -> tuple[PermissionVerdict, str | None]:
        descriptor = kwargs["descriptor"]
        assert kwargs["decide_only"] is True
        assert descriptor.safety_level == "dangerous"
        assert descriptor.requires_approval is False
        seen.append(descriptor.op_id)
        return answer["verdict"], "a reason"

    monkeypatch.setattr(_preview_steps, "policy_gate", fake_gate)

    waiting = await preview_step_verdict(
        operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
    )
    assert seen == ["PATCH:/things/{id}"]
    assert waiting is not None
    assert waiting[0] is PermissionVerdict.NEEDS_APPROVAL
    assert "PATCH:/things/{id}" in waiting[1]

    answer["verdict"] = PermissionVerdict.DENY
    denied = await preview_step_verdict(
        operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
    )
    assert denied == (PermissionVerdict.DENY, "the step PATCH:/things/{id} is denied: a reason")

    answer["verdict"] = PermissionVerdict.AUTO_EXECUTE
    assert (
        await preview_step_verdict(
            operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
        )
        is None
    )


@pytest.mark.asyncio
async def test_step_check_goes_into_a_step_that_has_its_own_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A lists step B; B is itself multi-step and lists the write step C.
    # Only A and B have grants, so C waits and the preview must say so.
    middle = "demo.composite.thing.move"
    inner = "POST:/things/{id}?action=relocate"
    register_governed_subops(
        composite_op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, sub_op_ids=(middle,)
    )
    register_governed_subops(
        composite_op_id=middle,
        connector_id=_STEP_CONNECTOR,
        # The loop back to A must not make the check run forever.
        sub_op_ids=(inner, _COMPOSITE),
    )
    granted = {_COMPOSITE, middle}
    seen: list[str] = []

    async def fake_gate(**kwargs: Any) -> tuple[PermissionVerdict, str | None]:
        op_id = kwargs["descriptor"].op_id
        seen.append(op_id)
        if op_id in granted:
            return PermissionVerdict.AUTO_EXECUTE, None
        return PermissionVerdict.NEEDS_APPROVAL, "no grant"

    monkeypatch.setattr(_preview_steps, "policy_gate", fake_gate)

    waiting = await preview_step_verdict(
        operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
    )
    assert seen == [middle, inner]
    assert waiting is not None
    assert waiting[0] is PermissionVerdict.NEEDS_APPROVAL
    assert inner in waiting[1]
    assert middle not in waiting[1]
    assert waiting[1].startswith("these steps have no grant")

    granted.add(inner)
    assert (
        await preview_step_verdict(
            operator=_service(), op_id=_COMPOSITE, connector_id=_STEP_CONNECTOR, target=None
        )
        is None
    )
