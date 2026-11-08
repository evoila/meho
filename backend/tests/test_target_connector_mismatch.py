# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A connector-bound op called on a target of another kind (#5948).

``dispatch`` refuses it with ``target_connector_mismatch`` right after the
target check, before the policy gate and any wait for approval, and writes
one audit row. ``preview_dispatch`` gives the same answer without an audit
row. A handler on a base class, or a subclass instance of the handler's
class, still runs. The ``target_required`` and ``no_connector`` answers do
not change.

The connectors, the products and the targets here are made up.
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
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._target_connector_match import (
    TARGET_CONNECTOR_MISMATCH,
    target_connector_mismatch,
)
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000c5c5")

#: The handlers that ran, so a test can prove a refused call ran nothing.
_RUNS: list[str] = []


class _ShellBase(Connector):
    """Made-up base class shared by the two shell connectors."""

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def uptime(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        del target, params
        _RUNS.append(f"{type(self).__name__}.uptime")
        return {"uptime_s": 1}


class _HostConnector(_ShellBase):
    product = "hostos"
    version = "1.0"
    impl_id = "hostos-ssh"

    async def about(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        del target, params
        _RUNS.append(f"{type(self).__name__}.about")
        return {"kind": "hostos"}


class _ClusterConnector(_ShellBase):
    product = "clusteros"
    version = "1.0"
    impl_id = "clusteros-ssh"


class _SeededHostConnector(_HostConnector):
    """The test-seeding pattern: a subclass registered under the parent's key."""


class _HostAgentConnector(_ShellBase):
    """A second connector of the same product, picked by ``preferred_impl_id``."""

    product = "hostos"
    version = "1.0"
    impl_id = "hostos-agent"


async def _module_level_op(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del target, params
    _RUNS.append("module_level_op")
    return {"ok": True}


class _Target:
    def __init__(self, product: str, *, preferred_impl_id: str | None = None) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = f"{product}-target-a"
        self.host = f"{product}-a.example.invalid"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.product = product
        self.preferred_impl_id = preferred_impl_id
        self.fingerprint = None
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
    register_connector_v2(product="hostos", version="1.0", impl_id="hostos-ssh", cls=_HostConnector)
    register_connector_v2(
        product="clusteros", version="1.0", impl_id="clusteros-ssh", cls=_ClusterConnector
    )
    yield
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()


def _operator() -> Operator:
    return Operator(
        sub="op-mismatch",
        name="Mismatch Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _register(op_id: str, handler: Any, *, requires_approval: bool = False) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product="hostos",
        version="1.0",
        impl_id="hostos-ssh",
        op_id=op_id,
        handler=handler,
        summary=f"{op_id} summary.",
        description=f"{op_id} description.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        safety_level="caution" if requires_approval else "safe",
        requires_approval=requires_approval,
        embedding_service=embedding,
    )


async def _call(op_id: str, target: Any) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id="hostos-ssh-1.0",
        op_id=op_id,
        target=target,
        params={},
    )


async def _audit_statuses(op_id: str) -> list[Any]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row.payload.get("result_status") for row in rows]


async def _approval_rows() -> int:
    async with get_sessionmaker()() as s:
        rows = (
            (await s.execute(select(ApprovalRequest).where(ApprovalRequest.tenant_id == _TENANT)))
            .scalars()
            .all()
        )
    return len(rows)


@pytest.mark.asyncio
async def test_host_op_on_cluster_target_is_refused_with_a_clear_message() -> None:
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", _Target("clusteros"))
    assert result.status == "error"
    assert result.extras["error_code"] == TARGET_CONNECTOR_MISMATCH
    assert result.extras["target_product"] == "clusteros"
    assert result.extras["operation_product"] == "hostos"
    assert result.extras["target_connector_id"] == "clusteros-ssh-1.0"
    assert result.extras["operation_connector_id"] == "hostos-ssh-1.0"
    assert result.error is not None
    assert result.error.startswith(f"{TARGET_CONNECTOR_MISMATCH}: ")
    assert "is of kind clusteros" in result.error
    assert "needs a target of kind hostos" in result.error
    assert "still unbound" not in result.error
    assert _RUNS == []
    assert await _audit_statuses("hostos.about") == [TARGET_CONNECTOR_MISMATCH]
    assert result.audit_id is not None


@pytest.mark.asyncio
async def test_write_that_needs_approval_is_refused_before_any_wait() -> None:
    await _register("hostos.reboot", _HostConnector.about, requires_approval=True)
    result = await _call("hostos.reboot", _Target("clusteros"))
    assert result.status == "error"
    assert result.extras["error_code"] == TARGET_CONNECTOR_MISMATCH
    assert await _approval_rows() == 0
    assert _RUNS == []


@pytest.mark.asyncio
async def test_approved_resume_gets_the_same_refusal() -> None:
    await _register("hostos.reboot", _HostConnector.about, requires_approval=True)
    result = await dispatch(
        operator=_operator(),
        connector_id="hostos-ssh-1.0",
        op_id="hostos.reboot",
        target=_Target("clusteros"),
        params={},
        _approved=True,
    )
    assert result.extras["error_code"] == TARGET_CONNECTOR_MISMATCH
    assert _RUNS == []


@pytest.mark.asyncio
async def test_matching_target_still_runs() -> None:
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", _Target("hostos"))
    assert result.status == "ok", result.error
    assert _RUNS == ["_HostConnector.about"]


@pytest.mark.asyncio
async def test_handler_on_a_base_class_runs_on_each_subclass() -> None:
    await _register("hostos.uptime", _HostConnector.uptime)
    result = await _call("hostos.uptime", _Target("clusteros"))
    assert result.status == "ok", result.error
    assert _RUNS == ["_ClusterConnector.uptime"]


@pytest.mark.asyncio
async def test_subclass_instance_under_the_parent_key_still_runs() -> None:
    clear_registry()
    register_connector_v2(
        product="hostos", version="1.0", impl_id="hostos-ssh", cls=_SeededHostConnector
    )
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", _Target("hostos"))
    assert result.status == "ok", result.error
    assert _RUNS == ["_SeededHostConnector.about"]


@pytest.mark.asyncio
async def test_other_connector_of_the_same_product_names_both_connectors() -> None:
    register_connector_v2(
        product="hostos", version="1.0", impl_id="hostos-agent", cls=_HostAgentConnector
    )
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", _Target("hostos", preferred_impl_id="hostos-agent"))
    assert result.extras["error_code"] == TARGET_CONNECTOR_MISMATCH
    assert result.extras["target_connector_id"] == "hostos-agent-1.0"
    assert result.error is not None
    assert "runs on connector hostos-agent-1.0" in result.error
    assert "preferred_impl_id to hostos-ssh" in result.error
    assert _RUNS == []


@pytest.mark.asyncio
async def test_no_target_answer_does_not_change() -> None:
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", None)
    assert result.extras["error_code"] == "target_required"


@pytest.mark.asyncio
async def test_no_connector_answer_does_not_change() -> None:
    await _register("hostos.about", _HostConnector.about)
    result = await _call("hostos.about", _Target("unknownos"))
    assert result.extras["error_code"] == "no_connector"
    assert _RUNS == []


@pytest.mark.asyncio
async def test_module_level_handler_is_never_refused() -> None:
    await _register("hostos.module_op", _module_level_op)
    result = await _call("hostos.module_op", _Target("clusteros"))
    assert result.status == "ok", result.error
    assert _RUNS == ["module_level_op"]


@pytest.mark.asyncio
async def test_preview_gives_the_same_refusal_without_an_audit_row() -> None:
    await _register("hostos.reboot", _HostConnector.about, requires_approval=True)
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id="hostos-ssh-1.0",
        op_id="hostos.reboot",
        target=_Target("clusteros"),
        params={},
    )
    assert preview["status"] == "error"
    assert preview["extras"]["error_code"] == TARGET_CONNECTOR_MISMATCH
    assert "is of kind clusteros" in preview["error"]
    assert await _audit_statuses("hostos.reboot") == []


def test_unimportable_handler_is_left_to_the_run_step() -> None:
    descriptor = AsyncMock()
    descriptor.source_kind = "typed"
    descriptor.handler_ref = "no_such_module_for_test.Thing.about"
    assert target_connector_mismatch(descriptor, _Target("clusteros")) is None
    descriptor.source_kind = "ingested"
    descriptor.handler_ref = f"{__name__}._HostConnector.about"
    assert target_connector_mismatch(descriptor, _Target("clusteros")) is None
