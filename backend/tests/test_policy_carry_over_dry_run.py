# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""What the dry run of the "always wait" carry-over lists.

``GET /api/v1/policy/carry-over/dry-run`` lists each saved "always wait"
of the scope: overrides with approval on and policy file entries with
``always_wait: true``. For each one it shows the source, the scope, the
operations it matches and the warnings: what changes if MEHO reads the
setting as ``destructive`` (a step of a multi-step operation, sandbox
mode, the preview).

* A policy file entry is matched against the operations a call of the
  tenant can use (not a turned-off one), and against every declared step
  with the group the step gate finds for it, also a step with no row a
  call can use.
* A step is matched by connector and op id, and names every multi-step
  operation that runs it, at every level.
* A tenant's answer holds nothing of another tenant.

Who may run it, that it writes only its audit row, and that an override
keeps waiting: ``test_policy_carry_over_dry_run_access.py``.

Drives the production ``meho_backplane.main:app`` with real tokens. DB is
the autouse per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from meho_backplane import op_classes
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    EndpointDescriptor,
    ServicePrincipalGrant,
    TenantOperationSetting,
)
from meho_backplane.main import app
from meho_backplane.policy.carry_over import DRY_RUN_MESSAGE
from meho_backplane.policy.store import classify_step
from meho_backplane.sandbox_mode import set_sandbox_mode_active
from meho_backplane.settings import get_settings

from ._carry_over_helpers import (
    CONNECTOR,
    NO_OPERATION,
    OTHER_TENANT,
    PREVIEW,
    SANDBOX,
    TENANT,
    add_steps,
    call,
    dry_run,
    op,
    override,
    policy,
    register,
    setup,
    step_text,
    tenant_row,
)
from ._oidc_jwt_helpers import make_rsa_keypair
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._time_limits import check_time_limit
from ._vault_fakes import install_fake_vault


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


@pytest.fixture
def sandbox() -> Iterator[None]:
    set_sandbox_mode_active(True)
    yield
    set_sandbox_mode_active(False)


@pytest.fixture
def classification_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()


@pytest.fixture
def a_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """``cowapi.vm.update`` is a step of one multi-step operation."""
    add_steps(monkeypatch, "cowapi.composite.rebuild", CONNECTOR, "cowapi.vm.update")


def _admin_key(name: str) -> object:
    return make_rsa_keypair(f"kid-co-{name}")


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox", "a_step")
async def test_the_dry_run_lists_each_saved_always_wait_of_the_tenant(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup(embedding)
    resp = dry_run(client, _admin_key("list"))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["scope"], body["sandbox_mode"], body["message"]) == (
        "tenant",
        True,
        DRY_RUN_MESSAGE,
    )
    the_override, group_entry, no_match = body["settings"]

    assert the_override["source"] == "override" and the_override["scope"] == "tenant"
    assert the_override["ref"] == "tenant:override"
    assert the_override["match"] == {"connector": CONNECTOR, "op": "cowapi.vm.update"}
    assert the_override["operations"] == [op("cowapi.vm.update", "cowapi.composite.rebuild")]
    assert the_override["warnings"] == [
        step_text("cowapi.vm.update", "cowapi.composite.rebuild"),
        SANDBOX,
        PREVIEW,
    ]

    # The group entry: vm.list has a more specific `always_wait: false`.
    assert group_entry["source"] == "policy_file" and group_entry["scope"] == "tenant"
    assert group_entry["ref"] == "tenant:classifications[0]"
    assert group_entry["match"] == {"connector": "cowapi-*", "group": "compute"}
    assert group_entry["operations"] == [op("cowapi.vm.update", "cowapi.composite.rebuild")]
    assert len(group_entry["warnings"]) == 3

    assert no_match["ref"] == "tenant:classifications[2]"
    assert no_match["operations"] == []
    assert no_match["warnings"] == [NO_OPERATION, SANDBOX, PREVIEW]


@pytest.mark.asyncio
async def test_outside_sandbox_mode_and_without_steps_only_the_preview_warns(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup(embedding)
    body = dry_run(client, _admin_key("plain")).json()
    assert body["sandbox_mode"] is False
    the_override = body["settings"][0]
    assert the_override["operations"] == [op("cowapi.vm.update")]
    assert the_override["warnings"] == [PREVIEW]


@pytest.mark.asyncio
async def test_a_tenant_without_saved_settings_gets_an_empty_list(client: TestClient) -> None:
    body = dry_run(client, _admin_key("empty")).json()
    assert body["settings"] == []
    assert body["message"] == DRY_RUN_MESSAGE


@pytest.mark.asyncio
async def test_a_stored_policy_that_is_not_valid_is_409(client: TestClient) -> None:
    await policy(TENANT, [{"match": "everything", "always_wait": True}])
    resp = dry_run(client, _admin_key("bad"))
    assert resp.status_code == 409
    assert "Import a fixed policy file." in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Steps of multi-step operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("classification_on")
async def test_a_step_with_no_stored_row_is_listed_under_a_matching_entry(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_steps(monkeypatch, "cowapi.composite.rebuild", CONNECTOR, "cowapi.hidden.step")
    await policy(
        TENANT,
        [
            {"match": {"op": "cowapi.hidden.*"}, "always_wait": True},
            # A group entry needs a stored row; the step has none.
            {"match": {"group": "hidden"}, "always_wait": True},
        ],
    )
    by_op, by_group = dry_run(client, _admin_key("hidden")).json()["settings"]

    assert by_op["operations"] == [op("cowapi.hidden.step", "cowapi.composite.rebuild")]
    assert by_op["warnings"] == [
        step_text("cowapi.hidden.step", "cowapi.composite.rebuild"),
        PREVIEW,
    ]
    assert NO_OPERATION not in by_op["warnings"]
    assert by_group["operations"] == []
    assert by_group["warnings"] == [NO_OPERATION, PREVIEW]


@pytest.mark.asyncio
async def test_a_step_is_matched_by_its_connector_too(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(embedding, "POST:/items", "compute", "caution")
    await register(embedding, "POST:/items", "compute", "caution", product="bullapi")
    add_steps(monkeypatch, "bullapi.composite.sync", "bullapi-1.x", "POST:/items")
    await override("POST:/items", TENANT, approval=True)
    await override("POST:/items", TENANT, approval=True, product="bullapi")

    bull, cow = dry_run(client, _admin_key("two")).json()["settings"]

    assert cow["operations"] == [op("POST:/items")]
    assert cow["warnings"] == [PREVIEW]
    assert bull["operations"] == [
        op("POST:/items", "bullapi.composite.sync", connector="bullapi-1.x")
    ]
    assert bull["warnings"] == [step_text("POST:/items", "bullapi.composite.sync"), PREVIEW]


@pytest.mark.asyncio
async def test_a_step_names_every_multi_step_operation_at_every_level(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    add_steps(monkeypatch, "cowapi.composite.outer", CONNECTOR, "cowapi.composite.inner")
    add_steps(monkeypatch, "cowapi.composite.inner", CONNECTOR, "cowapi.vm.update")
    # A list that loops back.
    add_steps(monkeypatch, "cowapi.loop.a", CONNECTOR, "cowapi.loop.b")
    add_steps(monkeypatch, "cowapi.loop.b", CONNECTOR, "cowapi.loop.a")
    await override("cowapi.vm.update", TENANT, approval=True)
    await override("cowapi.loop.a", TENANT, approval=True)

    loop, update = dry_run(client, _admin_key("nested")).json()["settings"]

    both = ("cowapi.composite.inner", "cowapi.composite.outer")
    assert update["operations"] == [op("cowapi.vm.update", *both)]
    assert update["warnings"] == [step_text("cowapi.vm.update", *both), PREVIEW]
    assert loop["operations"] == [op("cowapi.loop.a", "cowapi.loop.b")]
    assert loop["warnings"] == [step_text("cowapi.loop.a", "cowapi.loop.b"), PREVIEW]


# ---------------------------------------------------------------------------
# The operations of a tenant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("classification_on")
async def test_with_a_tenant_copy_only_its_own_operations_count(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    await tenant_row("cowapi.vm.list", TENANT)
    await policy(TENANT, [{"match": {"group": "compute"}, "always_wait": True}])
    key = _admin_key("copy")

    (entry,) = dry_run(client, key).json()["settings"]
    assert entry["operations"] == []
    assert entry["warnings"] == [NO_OPERATION, PREVIEW]

    listed = call(client, key, "cowapi.vm.list")
    assert listed["status"] == "ok", listed
    missing = call(client, key, "cowapi.vm.update")
    assert missing["status"] == "error" and missing["error"].startswith("unknown_op"), missing


@pytest.mark.asyncio
@pytest.mark.usefixtures("classification_on")
async def test_a_step_gets_its_group_the_way_the_step_gate_finds_it(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    await tenant_row("cowapi.vm.list", TENANT)
    add_steps(monkeypatch, "cowapi.composite.rebuild", CONNECTOR, "cowapi.vm.update")
    await policy(TENANT, [{"match": {"group": "compute"}, "always_wait": True}])

    (entry,) = dry_run(client, _admin_key("step-group")).json()["settings"]

    assert entry["operations"] == [op("cowapi.vm.update", "cowapi.composite.rebuild")]
    assert entry["warnings"] == [
        step_text("cowapi.vm.update", "cowapi.composite.rebuild"),
        PREVIEW,
    ]
    step = EndpointDescriptor(
        product="cowapi", version="1.x", impl_id="cowapi", op_id="cowapi.vm.update"
    )
    async with get_sessionmaker()() as session:
        classified = await classify_step(session, tenant_id=TENANT, step=step)
    assert classified.always_wait is True


async def _turn_off_built_in(op_id: str) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.tenant_id.is_(None), EndpointDescriptor.op_id == op_id)
            .values(is_enabled=False)
        )
        await session.commit()


async def _turn_off_for_tenant(op_id: str) -> None:
    async with get_sessionmaker()() as session:
        session.add(
            TenantOperationSetting(
                tenant_id=TENANT,
                product="cowapi",
                version="1.x",
                impl_id="cowapi",
                op_id=op_id,
                enabled=False,
                set_by_sub="admin-a",
                set_at=datetime.now(UTC),
            )
        )
        await session.commit()


@pytest.mark.asyncio
@pytest.mark.usefixtures("classification_on")
@pytest.mark.parametrize("turn_off", [_turn_off_built_in, _turn_off_for_tenant])
async def test_a_turned_off_operation_is_not_listed_as_a_call(
    client: TestClient, embedding: AsyncMock, turn_off: Any
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await turn_off("cowapi.vm.list")
    await policy(TENANT, [{"match": {"op": "cowapi.vm.list"}, "always_wait": True}])
    key = _admin_key("off")

    (entry,) = dry_run(client, key).json()["settings"]
    assert entry["operations"] == []
    assert entry["warnings"] == [NO_OPERATION, PREVIEW]

    answer = call(client, key, "cowapi.vm.list")
    assert answer["status"] == "error" and answer["error"].startswith("unknown_op"), answer


@pytest.mark.asyncio
async def test_a_tenant_answer_holds_nothing_of_another_tenant(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    await tenant_row("POST:/b-only", OTHER_TENANT)
    await override("cowapi.vm.update", TENANT, approval=True)
    await override("POST:/b-only", OTHER_TENANT, approval=True)
    await policy(TENANT, [{"match": {"op": "*"}, "always_wait": True}])
    await policy(OTHER_TENANT, [{"match": {"op": "POST:*"}, "always_wait": True}])
    key = _admin_key("tenants")

    a = dry_run(client, key).json()["settings"]
    b = dry_run(client, key, tenant=OTHER_TENANT, sub="admin-b").json()["settings"]

    assert [(s["source"], s["match"].get("op")) for s in a] == [
        ("override", "cowapi.vm.update"),
        ("policy_file", "*"),
    ]
    assert a[1]["operations"] == [op("cowapi.vm.list"), op("cowapi.vm.update")]
    # Tenant B has its own copy of the connector: only its own row counts.
    assert [(s["source"], s["match"].get("op")) for s in b] == [
        ("override", "POST:/b-only"),
        ("policy_file", "POST:*"),
    ]
    assert b[1]["operations"] == [op("POST:/b-only")]


# ---------------------------------------------------------------------------
# Each operation once, also for a broad entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_an_operation_stored_in_two_groups_is_listed_once(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.list", "compute", "safe")
    await tenant_row("cowapi.vm.list", OTHER_TENANT)
    await policy(None, [{"match": {"connector": "cowapi-*"}, "always_wait": True}])

    body = dry_run(client, _admin_key("twice"), "install", sub="root-a", platform_admin=True)
    (entry,) = body.json()["settings"]
    assert entry["operations"] == [op("cowapi.vm.list")]


@pytest.mark.asyncio
@pytest.mark.slow_runner_tolerant
async def test_one_entry_that_matches_3000_operations_answers_in_time(
    client: TestClient,
) -> None:
    async with get_sessionmaker()() as session:
        session.add_all(
            EndpointDescriptor(
                tenant_id=None,
                product="cowapi",
                version="1.x",
                impl_id="cowapi",
                op_id=f"cowapi.bulk.op{n:04d}",
                source_kind="typed",
                safety_level="safe",
                requires_approval=False,
                parameter_schema={"type": "object", "properties": {}},
            )
            for n in range(3000)
        )
        await session.commit()
    await policy(TENANT, [{"match": {"op": "cowapi.bulk.*"}, "always_wait": True}])

    started = time.monotonic()
    resp = dry_run(client, _admin_key("bulk"))
    elapsed = time.monotonic() - started

    assert resp.status_code == 200
    (entry,) = resp.json()["settings"]
    assert len(entry["operations"]) == 3000
    check_time_limit(elapsed, 10.0, f"the dry run took {elapsed:.1f} s for 3,000 operations")


# ---------------------------------------------------------------------------
# The texts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_no_warning_says_that_no_grant_covers_an_override(
    client: TestClient, embedding: AsyncMock
) -> None:
    await register(embedding, "cowapi.vm.update", "compute", "caution")
    await override("cowapi.vm.update", TENANT, approval=True)
    async with get_sessionmaker()() as session:
        session.add(
            ServicePrincipalGrant(
                id=uuid.uuid4(),
                tenant_id=TENANT,
                principal_sub="svc-a",
                op_id="cowapi.vm.update",
                connector_id=CONNECTOR,
                target_id=None,
                reason="fixture grant",
                created_by_sub="admin-a",
            )
        )
        await session.commit()
    key = _admin_key("grant")

    assert call(client, key, "cowapi.vm.update", sub="svc-a", principal_kind="service")[
        "status"
    ] == ("ok")
    (setting,) = dry_run(client, key).json()["settings"]
    assert setting["warnings"] == [SANDBOX, PREVIEW]
    assert not any("no grant" in w.lower() for w in setting["warnings"])


@pytest.mark.asyncio
@pytest.mark.usefixtures("sandbox")
async def test_the_sandbox_warning_promises_no_grant_for_a_credential_operation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(op_classes, "_CREDENTIAL_CAPABLE", {"cowapi.secret.rotate"})
    assert op_classes.is_credential_capable("cowapi.secret.rotate")
    await override("cowapi.secret.rotate", TENANT, approval=True)

    (setting,) = dry_run(client, _admin_key("secret")).json()["settings"]

    assert setting["warnings"] == [SANDBOX, PREVIEW]
    assert SANDBOX == (
        "As `destructive`: this install is in sandbox mode. There a delete grant of a service "
        "login can cover some `destructive` operations, but not every one."
    )
    assert not any("cowapi.secret.rotate" in w or "the operation" in w for w in setting["warnings"])


def test_an_operator_token_is_refused(client: TestClient) -> None:
    key = _admin_key("operator")
    resp = dry_run(client, key, tenant_role=TenantRole.OPERATOR.value)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "insufficient_role"
