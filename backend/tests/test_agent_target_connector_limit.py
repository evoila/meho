# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A hosted agent with a connector list may use only targets of those connectors.

The dispatcher picks the connector that runs a call from the target. With
the agent's list bound (``connector_limit``), ``dispatch`` and
``preview_dispatch`` refuse a target whose connector is not in the list,
or that resolves no single connector, with ``connector_not_allowed``. This
holds for an operation from an API file, a connector method (also one on a
base class two connectors share), a module-level function, and a step of a
multi-step operation. The refusal writes one audit row, makes no approval
request and runs nothing. Without a list, and for a target of an allowed
connector, the calls run as without the check.

The connectors, the products and the targets here are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy import select

import meho_backplane.agent.toolset as toolset_mod
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations import (
    dispatch,
    meta_tools,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.operations._target_connector_limit import (
    allowed_connectors_var,
    connector_limit,
)
from meho_backplane.operations.deny_reasons import CONNECTOR_NOT_ALLOWED
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-0000000013a7")
_HOST = "hostos-ssh-1.0"
_ONLY_HOST = frozenset({_HOST})

#: What ran, so a test can prove a refused call ran nothing.
_RUNS: list[str] = []


class _ShellBase(Connector):
    """Made-up base class shared by the two shell connectors."""

    accepts_ingested_operations = True

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def uptime(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        del params
        _RUNS.append(f"uptime:{target.name}")
        return {"uptime_s": 1}


class _HostConnector(_ShellBase):
    product = "hostos"
    version = "1.0"
    impl_id = "hostos-ssh"

    async def about(self, target: Any, params: dict[str, Any]) -> dict[str, Any]:
        del params
        _RUNS.append(f"about:{target.name}")
        return {"kind": "hostos"}

    async def check_both(
        self, operator: Operator, target: Any, params: dict[str, Any], dispatch_child: Any
    ) -> dict[str, Any]:
        """A multi-step operation: one step on the host, one on a cluster target."""
        del operator, params
        own = await dispatch_child(connector_id=_HOST, op_id="hostos.uptime", params={})
        other = await dispatch_child(
            connector_id=_HOST, op_id="hostos.uptime", params={}, target=_Target("clusteros")
        )
        _RUNS.append(f"check_both:{target.name}")
        return {
            "own": own.status,
            "other": other.status,
            "other_code": other.extras.get("error_code"),
        }


class _ClusterConnector(_ShellBase):
    product = "clusteros"
    version = "1.0"
    impl_id = "clusteros-ssh"


async def _module_level_op(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    del params
    _RUNS.append(f"module:{target.name}")
    return {"ok": True}


class _Target:
    def __init__(self, product: str) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = f"{product}-target-a"
        self.host = f"{product}-a.example.invalid"
        self.port = 22
        self.auth_model = "shared_service_account"
        self.product = product
        self.preferred_impl_id = None
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
        sub="agent:limited",
        name="Limited Agent",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.AGENT,
        client_id="agent:limited",
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


async def _add_descriptor(op_id: str, **fields: Any) -> None:
    now = datetime.now(UTC)
    async with get_sessionmaker()() as s:
        s.add(
            EndpointDescriptor(
                id=uuid.uuid4(),
                tenant_id=None,
                product="hostos",
                version="1.0",
                impl_id="hostos-ssh",
                op_id=op_id,
                summary=f"{op_id} summary.",
                description=f"{op_id} description.",
                tags=[],
                parameter_schema={"type": "object"},
                response_schema=None,
                llm_instructions=None,
                safety_level="safe",
                requires_approval=False,
                is_enabled=True,
                embedding=[0.1] * 384,
                custom_description=None,
                custom_notes=None,
                created_at=now,
                updated_at=now,
                **fields,
            )
        )
        await s.commit()


async def _add_api_file_op(op_id: str) -> None:
    """An operation ingested from an API file, stored under the host connector."""
    await _add_descriptor(
        op_id, source_kind="ingested", method="GET", path="/about", handler_ref=None
    )


async def _add_composite(op_id: str) -> None:
    await _add_descriptor(
        op_id,
        source_kind="composite",
        method=None,
        path=None,
        handler_ref=f"{__name__}._HostConnector.check_both",
    )


async def _call(op_id: str, target: Any) -> Any:
    return await dispatch(
        operator=_operator(), connector_id=_HOST, op_id=op_id, target=target, params={}
    )


async def _preview(op_id: str, target: Any) -> Any:
    return await preview_dispatch(
        operator=_operator(), connector_id=_HOST, op_id=op_id, target=target, params={}
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


def _assert_refused(result: Any, picked: str | None) -> None:
    assert result.status == "error"
    assert result.extras["error_code"] == CONNECTOR_NOT_ALLOWED
    assert result.extras["target_connector_id"] == picked
    assert result.extras["allowed_connectors"] == [_HOST]
    assert result.error.startswith(f"{CONNECTOR_NOT_ALLOWED}: ")


# --- the three kinds of operation, call and preview ------------------------


@pytest.mark.asyncio
async def test_api_file_op_on_other_connector_target_is_refused() -> None:
    await _add_api_file_op("hostos.get_about")
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.get_about", _Target("clusteros"))
        preview = await _preview("hostos.get_about", _Target("clusteros"))
    _assert_refused(result, "clusteros-ssh-1.0")
    assert "runs on connector clusteros-ssh-1.0" in result.error
    assert preview["status"] == "error"
    assert preview["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert await _audit_statuses("hostos.get_about") == [CONNECTOR_NOT_ALLOWED]


@pytest.mark.asyncio
async def test_method_on_a_shared_base_class_is_refused_on_other_connector_target() -> None:
    await _register("hostos.uptime", _HostConnector.uptime)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.uptime", _Target("clusteros"))
        preview = await _preview("hostos.uptime", _Target("clusteros"))
    _assert_refused(result, "clusteros-ssh-1.0")
    assert preview["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert _RUNS == []
    assert await _audit_statuses("hostos.uptime") == [CONNECTOR_NOT_ALLOWED]


@pytest.mark.asyncio
async def test_method_only_on_the_own_class_keeps_the_mismatch_answer() -> None:
    await _register("hostos.about", _HostConnector.about)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.about", _Target("clusteros"))
    assert result.extras["error_code"] == "target_connector_mismatch"
    assert _RUNS == []


@pytest.mark.asyncio
async def test_module_level_function_is_refused_on_other_connector_target() -> None:
    await _register("hostos.module_op", _module_level_op)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.module_op", _Target("clusteros"))
        preview = await _preview("hostos.module_op", _Target("clusteros"))
    _assert_refused(result, "clusteros-ssh-1.0")
    assert preview["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert _RUNS == []


@pytest.mark.asyncio
async def test_target_with_no_connector_is_refused() -> None:
    await _register("hostos.module_op", _module_level_op)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.module_op", _Target("unknownos"))
    _assert_refused(result, None)
    assert "does not resolve to one connector" in result.error
    assert _RUNS == []


@pytest.mark.asyncio
async def test_empty_list_refuses_every_target() -> None:
    await _register("hostos.module_op", _module_level_op)
    with connector_limit(frozenset()):
        result = await _call("hostos.module_op", _Target("hostos"))
    assert result.extras["error_code"] == CONNECTOR_NOT_ALLOWED
    assert "allowed connectors (none)" in result.error
    assert _RUNS == []


@pytest.mark.asyncio
async def test_op_that_needs_approval_is_refused_before_any_approval_request() -> None:
    await _register("hostos.reboot", _module_level_op, requires_approval=True)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.reboot", _Target("clusteros"))
    _assert_refused(result, "clusteros-ssh-1.0")
    assert await _approval_rows() == 0
    assert _RUNS == []


@pytest.mark.asyncio
async def test_step_of_a_multi_step_operation_is_checked() -> None:
    await _register("hostos.uptime", _HostConnector.uptime)
    await _add_composite("hostos.check_both")
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.check_both", _Target("hostos"))
    assert result.status == "ok", result.error
    assert result.result == {"own": "ok", "other": "error", "other_code": CONNECTOR_NOT_ALLOWED}
    assert _RUNS == ["uptime:hostos-target-a", "check_both:hostos-target-a"]


# --- what does not change ---------------------------------------------------


@pytest.mark.asyncio
async def test_allowed_connector_target_runs() -> None:
    await _register("hostos.uptime", _HostConnector.uptime)
    await _register("hostos.module_op", _module_level_op)
    with connector_limit(_ONLY_HOST):
        first = await _call("hostos.uptime", _Target("hostos"))
        second = await _call("hostos.module_op", _Target("hostos"))
        preview = await _preview("hostos.uptime", _Target("hostos"))
    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert preview.get("extras", {}).get("error_code") != CONNECTOR_NOT_ALLOWED
    assert _RUNS == ["uptime:hostos-target-a", "module:hostos-target-a"]


@pytest.mark.asyncio
async def test_no_list_keeps_every_answer() -> None:
    await _register("hostos.uptime", _HostConnector.uptime)
    await _register("hostos.module_op", _module_level_op)
    assert allowed_connectors_var.get() is None
    first = await _call("hostos.uptime", _Target("clusteros"))
    second = await _call("hostos.module_op", _Target("clusteros"))
    third = await _call("hostos.module_op", _Target("unknownos"))
    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert third.extras["error_code"] == "no_connector"
    assert _RUNS == ["uptime:clusteros-target-a", "module:clusteros-target-a"]


@pytest.mark.asyncio
async def test_no_target_is_not_checked() -> None:
    await _register("hostos.module_op", _module_level_op)
    with connector_limit(_ONLY_HOST):
        result = await _call("hostos.module_op", None)
    assert result.extras.get("error_code") != CONNECTOR_NOT_ALLOWED


# --- through the hosted agent's tools ---------------------------------------


def _unused_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    return ModelResponse(parts=[TextPart("unused")])


def _ctx(operator: Operator) -> RunContext[Operator]:
    return RunContext(deps=operator, model=FunctionModel(_unused_model), usage=RunUsage())


def _target_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve a target name to a made-up target, without a database row."""

    async def fake_resolve(
        operator: Operator, op_id: str, target_arg: Any, *, bind_audit_target: bool
    ) -> tuple[Any, None]:
        del operator, op_id, bind_audit_target
        return _Target(str(target_arg["name"]).removesuffix("-target-a")), None

    monkeypatch.setattr(meta_tools, "_resolve_target_or_error", fake_resolve)


def _tools(connectors: list[str] | None) -> dict[str, Any]:
    spec: dict[str, Any] = {"meta_tools": ["call_operation", "preview_operation"]}
    if connectors is not None:
        spec["connectors"] = connectors
    return {t.name: t for t in toolset_mod.resolve_agent_tools(spec, _operator())}


@pytest.mark.asyncio
async def test_agent_tools_refuse_a_target_of_another_connector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _target_by_name(monkeypatch)
    await _register("hostos.uptime", _HostConnector.uptime)
    tools = _tools([_HOST])
    args = {"connector_id": _HOST, "op_id": "hostos.uptime", "params": {}}
    called = await tools["call_operation"].function(
        _ctx(_operator()), target={"name": "clusteros-target-a"}, **args
    )
    previewed = await tools["preview_operation"].function(
        _ctx(_operator()), target={"name": "clusteros-target-a"}, **args
    )
    allowed = await tools["call_operation"].function(
        _ctx(_operator()), target={"name": "hostos-target-a"}, **args
    )
    assert called["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert previewed["extras"]["error_code"] == CONNECTOR_NOT_ALLOWED
    assert allowed["status"] == "ok"
    assert _RUNS == ["uptime:hostos-target-a"]
    assert allowed_connectors_var.get() is None


@pytest.mark.asyncio
async def test_agent_without_a_list_keeps_the_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _target_by_name(monkeypatch)
    await _register("hostos.uptime", _HostConnector.uptime)
    tools = _tools(None)
    result = await tools["call_operation"].function(
        _ctx(_operator()),
        connector_id=_HOST,
        op_id="hostos.uptime",
        target={"name": "clusteros-target-a"},
        params={},
    )
    assert result["status"] == "ok"
    assert _RUNS == ["uptime:clusteros-target-a"]
