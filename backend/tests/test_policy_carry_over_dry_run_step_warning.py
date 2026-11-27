# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The step warning of the carry-over dry run, in both scopes.

``step_of`` of an operation names a multi-step operation, with the step
warning, exactly when the setting makes that step wait for at least one
run of the scope, with the group the step check finds for the run: the
tenant's own row of the step first, then the built-in row. Each case
also asks the step check (:func:`classify_step`) of the tenants of the
case, so the dry run and the check give the same answer.

The connector ``cowapi-1.x`` has ``cowapi.vm.update`` (``safe``, group
``compute``) and ``cowapi.disk.list`` (``safe``, group ``storage``); the
multi-step operation ``cowapi.composite.rebuild`` lists the step
``cowapi.vm.update``. All names and values are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.main import app
from meho_backplane.operations.op_policy import apply_step_policy
from meho_backplane.policy.store import classify_step
from meho_backplane.settings import get_settings

from ._carry_over_helpers import (
    CONNECTOR,
    OTHER_TENANT,
    PREVIEW,
    TENANT,
    add_steps,
    dry_run,
    op,
    override,
    policy,
    register,
    step_text,
    tenant_row,
)
from ._oidc_jwt_helpers import make_rsa_keypair
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)
from ._vault_fakes import install_fake_vault

STEP = "cowapi.vm.update"
COMPOSITE = "cowapi.composite.rebuild"
WARNING = step_text(STEP, COMPOSITE)
IN_STORAGE = "storage"
IN_NO_GROUP = None


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


@pytest.fixture(autouse=True)
def classification_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def a_step(monkeypatch: pytest.MonkeyPatch) -> None:
    add_steps(monkeypatch, COMPOSITE, CONNECTOR, STEP)


async def _set_up(embedding: AsyncMock, *, tenant_a_step: str | bool | None = False) -> None:
    """The built-in rows; with *tenant_a_step* not ``False``, tenant A's own row of the step.

    *tenant_a_step* is the group of that row (``None``: in no group).
    """
    await register(embedding, STEP, "compute", "safe")
    await register(embedding, "cowapi.disk.list", "storage", "safe")
    if tenant_a_step is False:
        return
    await tenant_row(STEP, TENANT)
    if tenant_a_step is None:
        return
    async with get_sessionmaker()() as session:
        group_id = (
            await session.execute(
                select(EndpointDescriptor.group_id).where(
                    EndpointDescriptor.tenant_id.is_(None),
                    EndpointDescriptor.op_id == "cowapi.disk.list",
                )
            )
        ).scalar_one()
        await session.execute(
            update(EndpointDescriptor)
            .where(EndpointDescriptor.tenant_id == TENANT, EndpointDescriptor.op_id == STEP)
            .values(group_id=group_id)
        )
        await session.commit()


def _step_row() -> EndpointDescriptor:
    return EndpointDescriptor(
        product="cowapi",
        version="1.x",
        impl_id="cowapi",
        op_id=STEP,
        safety_level="safe",
        requires_approval=False,
    )


async def _step_waits(tenant_id: uuid.UUID) -> bool:
    async with get_sessionmaker()() as session:
        classified = await classify_step(session, tenant_id=tenant_id, step=_step_row())
    return classified.always_wait is True


def _install(client: TestClient, name: str) -> list[dict[str, Any]]:
    key = make_rsa_keypair(f"kid-sw-{name}")
    body = dry_run(client, key, "install", sub="root-a", platform_admin=True).json()
    settings: list[dict[str, Any]] = body["settings"]
    return settings


def _tenant(client: TestClient, name: str) -> list[dict[str, Any]]:
    settings: list[dict[str, Any]] = dry_run(client, make_rsa_keypair(f"kid-sw-{name}")).json()[
        "settings"
    ]
    return settings


# ---------------------------------------------------------------------------
# Install scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_17_install_group_entry_warns_for_the_group_of_a_tenant_row(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding, tenant_a_step=IN_STORAGE)
    await policy(None, [{"match": {"group": "storage"}, "always_wait": True}])

    (entry,) = _install(client, "17")

    assert entry["operations"] == [op("cowapi.disk.list"), op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    assert await _step_waits(TENANT) is True


@pytest.mark.asyncio
async def test_18_install_group_entry_warns_for_the_built_in_group(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding, tenant_a_step=IN_STORAGE)
    await policy(None, [{"match": {"group": "compute"}, "always_wait": True}])

    (entry,) = _install(client, "18")

    assert entry["operations"] == [op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    assert await _step_waits(OTHER_TENANT) is True
    assert await _step_waits(TENANT) is False


@pytest.mark.asyncio
async def test_19_install_group_entry_that_no_run_reaches_does_not_list_the_step(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding)
    await policy(None, [{"match": {"group": "storage"}, "always_wait": True}])

    (entry,) = _install(client, "19")

    assert entry["operations"] == [op("cowapi.disk.list")]
    assert entry["warnings"] == [PREVIEW]
    assert await _step_waits(TENANT) is False
    assert await _step_waits(OTHER_TENANT) is False


_CONNECTOR_BUT_NOT_COMPUTE = [
    {"match": {"connector": CONNECTOR}, "always_wait": True},
    {"match": {"group": "compute"}, "always_wait": False},
]


@pytest.mark.asyncio
async def test_20_install_connector_entry_warns_when_a_tenant_row_escapes_the_overrule(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding, tenant_a_step=IN_STORAGE)
    await policy(None, _CONNECTOR_BUT_NOT_COMPUTE)

    (entry,) = _install(client, "20")

    assert entry["operations"] == [op("cowapi.disk.list"), op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    assert await _step_waits(TENANT) is True
    assert await _step_waits(OTHER_TENANT) is False


@pytest.mark.asyncio
async def test_21_install_connector_entry_lists_a_tenant_call_without_a_step_warning(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding, tenant_a_step=IN_NO_GROUP)
    await policy(None, _CONNECTOR_BUT_NOT_COMPUTE)

    (entry,) = _install(client, "21")

    assert entry["operations"] == [op("cowapi.disk.list"), op(STEP)]
    assert entry["warnings"] == [PREVIEW]
    assert await _step_waits(TENANT) is False
    assert await _step_waits(OTHER_TENANT) is False


# ---------------------------------------------------------------------------
# Tenant scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_22_tenant_group_entry_warns_for_the_group_of_its_own_row(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding, tenant_a_step=IN_STORAGE)
    await policy(TENANT, [{"match": {"group": "storage"}, "always_wait": True}])

    (entry,) = _tenant(client, "22")

    assert entry["operations"] == [op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    assert await _step_waits(TENANT) is True


@pytest.mark.asyncio
async def test_23_tenant_copy_without_the_step_warns_for_the_built_in_group(
    client: TestClient, embedding: AsyncMock
) -> None:
    await _set_up(embedding)
    await tenant_row("cowapi.disk.list", TENANT)
    await policy(TENANT, [{"match": {"group": "compute"}, "always_wait": True}])

    (entry,) = _tenant(client, "23")

    assert entry["operations"] == [op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    assert await _step_waits(TENANT) is True


# ---------------------------------------------------------------------------
# An override, in both scopes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["install", "tenant"])
async def test_24_an_override_with_approval_on_warns_for_the_step(
    client: TestClient, embedding: AsyncMock, scope: str
) -> None:
    await _set_up(embedding)
    before = await apply_step_policy(tenant_id=TENANT, step=_step_row())
    assert before.requires_approval is False

    await override(STEP, None if scope == "install" else TENANT, approval=True)
    (entry,) = _install(client, "24") if scope == "install" else _tenant(client, "24")

    assert entry["source"] == "override"
    assert entry["operations"] == [op(STEP, COMPOSITE)]
    assert entry["warnings"] == [WARNING, PREVIEW]
    after = await apply_step_policy(tenant_id=TENANT, step=_step_row())
    assert after.requires_approval is True
