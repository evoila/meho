# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``preview_operation`` says whether a live grant runs the call at once (#5492).

The harness is the one ``test_sandbox_mode_calls.py`` uses (the vm.destroy
connector double, the migrated SQLite engine, the embedding stub).

* With a delete grant for the operation and for each listed write step,
  the preview says ``auto-execute`` and names the grant; the real call
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
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EventOutbox, PermissionVerdict, ServicePrincipalGrant
from meho_backplane.operations import _preview_steps, reset_dispatcher_caches
from meho_backplane.operations._preview_steps import preview_step_verdict
from meho_backplane.operations.governed_subops import (
    register_governed_subops,
    reset_governed_subop_registry,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests._sandbox_mode_helpers import (
    grant_use_rows,
    seed_delete_grant,
    seed_grant_row,
    soon,
    turn_sandbox_mode_off,
    turn_sandbox_mode_on,
)
from tests.test_connectors_vmware_rest_vm_destroy import (
    _CONNECTOR_ID,
    _OP_ID,
    _PARAMS,
    _TENANT_ID,
    _bootstrap,
    _make_operator,
    _pending_count,
    _RecordingVmwareConnector,
    _seed_target,
    _seed_vm_reads,
)

_SERVICE_SUB = "svc:test-runner"
_REST_STEP = "DELETE:/vcenter/vm/{vm}"
_VIM_STEP = "POST:/VirtualMachine/{moId}/Destroy_Task"
_VIM_READ_STEP = "POST:/PropertyCollector/{moId}/RetrievePropertiesEx"
_ARGS: dict[str, Any] = {
    "connector_id": _CONNECTOR_ID,
    "op_id": _OP_ID,
    "target": "test-vcenter",
    "params": _PARAMS,
}
_GRANT_FIELDS: dict[str, Any] = {
    "tenant_id": _TENANT_ID,
    "principal_sub": _SERVICE_SUB,
    "connector_id": _CONNECTOR_ID,
    "target_product": "vmware",
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
    return _make_operator(sub=_SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)


async def _ready(stub_embedding_service: AsyncMock) -> _RecordingVmwareConnector:
    recorder = _RecordingVmwareConnector(about_version="9.0.0")
    _seed_vm_reads(recorder)
    await _bootstrap(recorder, stub_embedding_service)
    await _seed_target()
    return recorder


async def _grant_op_and_steps() -> uuid.UUID:
    """Grants for vm.destroy and each listed write step; returns the op's grant."""
    op_grant = await seed_delete_grant(op_id=_OP_ID, **_GRANT_FIELDS)
    await seed_delete_grant(op_id=_REST_STEP, **_GRANT_FIELDS)
    await seed_delete_grant(op_id=_VIM_STEP, **_GRANT_FIELDS)
    await seed_grant_row(model=ServicePrincipalGrant, op_id=_VIM_READ_STEP, **_GRANT_FIELDS)
    return op_grant


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
    assert _REST_STEP in reason
    assert _VIM_STEP in reason
    # The GET read of the VM is not a write step.
    assert "GET:/vcenter/vm/{vm}" not in reason
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
