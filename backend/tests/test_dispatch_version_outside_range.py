# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A target whose known version is outside its connector's range does not run.

The Kubernetes and RKE2 connectors are registered the way production does
it: one entry under their version, and one fallback entry that accepts
every version. The resolver picks the fallback for a 1.31 target. ``dispatch``
and ``preview_dispatch`` still refuse that target with ``no_connector``,
and the error names the range. A target in range, or with no known
version, still runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.kubernetes import KubernetesConnector
from meho_backplane.connectors.registry import (
    clear_registry,
    register_connector,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.rke2 import Rke2SshConnector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000d6d6")
_RANGE = ">=1.32,<1.38"
_RUNS: list[str] = []

_CONNECTORS = pytest.mark.parametrize(
    ("product", "impl_id", "cls"),
    [("k8s", "k8s", KubernetesConnector), ("rke2", "rke2-ssh", Rke2SshConnector)],
)


async def _read_op(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    _RUNS.append("read")
    return {"ok": True}


class _Target:
    def __init__(self, product: str, detected: str | None) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = f"{product}-target-a"
        self.host = f"{product}-a.example.invalid"
        self.port = 6443
        self.auth_model = "shared_service_account"
        self.product = product
        self.preferred_impl_id = None
        self.fingerprint = {"version": detected} if detected else None
        self.version = None


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _registry() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()
    # The same entries the two connector packages register in production.
    register_connector_v2(product="k8s", version="1.x", impl_id="k8s", cls=KubernetesConnector)
    register_connector("k8s", KubernetesConnector)
    register_connector_v2(product="rke2", version="1.x", impl_id="rke2-ssh", cls=Rke2SshConnector)
    register_connector_v2(product="rke2", version="", impl_id="", cls=Rke2SshConnector)
    yield
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()


def _operator() -> Operator:
    return Operator(
        sub="op-version-range",
        name="Version Range Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register(product: str, impl_id: str) -> str:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    op_id = f"{product}.range_test.read"
    await register_typed_operation(
        product=product,
        version="1.x",
        impl_id=impl_id,
        op_id=op_id,
        handler=_read_op,
        summary="Range test read.",
        description="Range test read.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="safe",
        requires_approval=False,
        embedding_service=embedding,
    )
    return op_id


async def _audit_statuses(op_id: str) -> list[Any]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row.payload.get("result_status") for row in rows]


@_CONNECTORS
def test_fallback_entry_still_picks_the_connector(
    product: str, impl_id: str, cls: type[Any]
) -> None:
    """The precondition: without the run-time check, a 1.31 target would run."""
    del impl_id
    assert resolve_connector(_Target(product, "v1.31.9")) is cls
    assert resolve_connector(_Target(product, None)) is cls


@_CONNECTORS
@pytest.mark.asyncio
async def test_version_below_range_is_refused_and_names_the_range(
    product: str, impl_id: str, cls: type[Any]
) -> None:
    del cls
    op_id = await _register(product, impl_id)
    result = await dispatch(
        operator=_operator(),
        connector_id=f"{impl_id}-1.x",
        op_id=op_id,
        target=_Target(product, "v1.31.9"),
        params={},
    )
    assert result.status == "error"
    assert result.extras["error_code"] == "no_connector"
    assert result.extras["supported_version_range"] == _RANGE
    assert result.extras["version"] == "v1.31.9"
    assert result.error is not None
    assert result.error.startswith("no_connector: ")
    assert _RANGE in result.error
    assert "v1.31.9" in result.error
    assert _RUNS == []
    assert await _audit_statuses(op_id) == ["no_connector"]


@_CONNECTORS
@pytest.mark.asyncio
async def test_preview_gives_the_same_refusal(product: str, impl_id: str, cls: type[Any]) -> None:
    del cls
    op_id = await _register(product, impl_id)
    envelope = await preview_dispatch(
        operator=_operator(),
        connector_id=f"{impl_id}-1.x",
        op_id=op_id,
        target=_Target(product, "v1.31.9"),
        params={},
    )
    assert envelope["status"] == "error"
    assert envelope["extras"]["error_code"] == "no_connector"
    assert _RANGE in envelope["error"]
    assert _RUNS == []


@_CONNECTORS
@pytest.mark.parametrize("detected", ["v1.35.0+rke2r1", None])
@pytest.mark.asyncio
async def test_version_in_range_or_unknown_still_runs(
    product: str, impl_id: str, cls: type[Any], detected: str | None
) -> None:
    del cls
    op_id = await _register(product, impl_id)
    result = await dispatch(
        operator=_operator(),
        connector_id=f"{impl_id}-1.x",
        op_id=op_id,
        target=_Target(product, detected),
        params={},
    )
    assert result.status == "ok", result.error
    assert _RUNS == ["read"]
