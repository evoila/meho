# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A health check (Sensor) never runs a ``safe`` write.

The five GitHub ticket writes are ``safe`` because they need no approval
(``docs/decisions/2026-11-02-github-ticket-writes-are-safe.md``), but they are
writes. A Sensor runs its operation on every tick with no person, so:

* the create guard refuses a Sensor on one of the five
  (:class:`SensorRequiresReadOperationError`, same 422 code as a non-safe op);
* the dispatch-time floor of the runner records ``unknown`` and never
  dispatches one, also for a Sensor that already exists.

Runs on the SQLite engine from :mod:`tests.conftest`. No live GitHub.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.checks.runner import _safe_tier_floor, _SensorSnapshot
from meho_backplane.checks.schemas import SensorCreate
from meho_backplane.checks.service import (
    SensorAdminService,
    SensorRequiresReadOperationError,
    SensorRequiresSafeOperationError,
)
from meho_backplane.connectors.github.op_classes import TICKET_WRITE_OP_IDS
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, Tenant
from meho_backplane.op_classes import declared_op_class
from meho_backplane.settings import get_settings

_TENANT = UUID("abababab-abab-abab-abab-abababababab")
_CONNECTOR_ID = "gh-rest-3"
_READ_OP = "gh.composite.issue_view"
_K8S_CONNECTOR_ID = "k8s-1.x"
_K8S_WRITE_OP = "k8s.secret.create"
_ASSERTION: dict[str, Any] = {
    "select": {"path": "$.count"},
    "compare": {"type": "threshold", "op": "lt", "critical": 10},
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed(
    op_id: str, *, product: str = "gh", version: str = "3", impl_id: str = "gh-rest"
) -> None:
    """A tenant and a global, enabled, ``safe`` composite row for *op_id*."""
    async with get_sessionmaker()() as session:
        if (await session.execute(select(Tenant).where(Tenant.id == _TENANT))).scalar() is None:
            session.add(Tenant(id=_TENANT, slug="tenant-gh", name="Tenant gh"))
        session.add(
            EndpointDescriptor(
                product=product,
                version=version,
                impl_id=impl_id,
                op_id=op_id,
                source_kind="composite",
                handler_ref=f"tests.fake:{op_id}",
                parameter_schema={"type": "object", "properties": {}},
                safety_level="safe",
                requires_approval=False,
            )
        )
        await session.commit()


def _payload(op_id: str, connector_id: str = _CONNECTOR_ID) -> SensorCreate:
    return SensorCreate.model_validate(
        {
            "name": f"sensor-{op_id}",
            "connector_id": connector_id,
            "op_id": op_id,
            "assertion": _ASSERTION,
            "cadence_kind": "interval",
            "interval_seconds": 60,
        }
    )


def _snapshot(op_id: str, connector_id: str = _CONNECTOR_ID) -> _SensorSnapshot:
    return _SensorSnapshot(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        name="s",
        connector_id=connector_id,
        op_id=op_id,
        target=None,
        params={},
        assertion=_ASSERTION,
        identity_sub="__sensor__",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", TICKET_WRITE_OP_IDS)
async def test_a_sensor_cannot_be_made_with_a_ticket_write(op_id: str) -> None:
    await _seed(op_id)
    with pytest.raises(SensorRequiresReadOperationError) as raised:
        await SensorAdminService().create(
            tenant_id=_TENANT, created_by_sub="op-admin", payload=_payload(op_id)
        )
    # Same class family and code as a non-safe op, so every transport maps it to 422.
    assert isinstance(raised.value, SensorRequiresSafeOperationError)
    assert raised.value.error_code == "sensor_requires_safe_operation"
    assert "is a write" in str(raised.value)


@pytest.mark.asyncio
async def test_a_sensor_on_a_safe_read_of_the_same_connector_is_still_allowed() -> None:
    await _seed(_READ_OP)
    entry = await SensorAdminService().create(
        tenant_id=_TENANT, created_by_sub="op-admin", payload=_payload(_READ_OP)
    )
    assert entry.name == f"sensor-{_READ_OP}"


@pytest.mark.asyncio
@pytest.mark.parametrize("op_id", TICKET_WRITE_OP_IDS)
async def test_the_runner_floor_never_dispatches_a_ticket_write(op_id: str) -> None:
    await _seed(op_id)
    outcome = await _safe_tier_floor(_snapshot(op_id))
    assert outcome is not None
    assert outcome.state == "unknown"
    assert outcome.evidence["reason"] == "op_is_write_at_dispatch"


@pytest.mark.asyncio
async def test_the_runner_floor_lets_a_safe_read_through() -> None:
    await _seed(_READ_OP)
    assert await _safe_tier_floor(_snapshot(_READ_OP)) is None


@pytest.mark.asyncio
async def test_a_safe_op_of_another_connector_declared_write_is_still_a_sensor_op() -> None:
    # The Kubernetes secret create is declared ``credential_write`` for the feed, but
    # it is not a safe write, so a Sensor may still use it and the runner runs it.
    import meho_backplane.connectors.kubernetes.op_classes  # noqa: F401

    assert declared_op_class(_K8S_WRITE_OP) == "credential_write"
    await _seed(_K8S_WRITE_OP, product="k8s", version="1.x", impl_id="k8s")
    entry = await SensorAdminService().create(
        tenant_id=_TENANT,
        created_by_sub="op-admin",
        payload=_payload(_K8S_WRITE_OP, _K8S_CONNECTOR_ID),
    )
    assert entry.name == f"sensor-{_K8S_WRITE_OP}"
    assert await _safe_tier_floor(_snapshot(_K8S_WRITE_OP, _K8S_CONNECTOR_ID)) is None
