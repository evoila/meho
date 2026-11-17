# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A tool profile with a connector list also covers the target (#6099).

The dispatcher runs a call on the connector that the target resolves to,
not on the ``connector_id`` argument. These tests check, with the real
dispatcher and real target rows:

* ``call_operation`` and ``preview_operation`` with a ``connector_id`` of
  the profile and a target of another connector are refused, for an
  operation from an API file, a typed operation whose handler is a class
  method, and one whose handler is a module-level function. Nothing is
  sent and no approval request is made;
* ``list_targets`` shows only the targets of the profile's connectors;
* a background run is read only under the profile that started it;
* every tool of a built-in tool list that may change something is in the
  write tool list.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.result_handle_store import result_profile_scope
from meho_backplane.connectors.schemas import FingerprintResult, OperationResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    ApprovalRequest,
    EndpointDescriptor,
    OperationRunOrigin,
    Tenant,
)
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.mcp.profiles import MEHO_WRITE_TOOLS, TOOL_PROFILES, profile_target_denial
from meho_backplane.mcp.registry import get_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools._call_operation_async import poll_call
from meho_backplane.operations import operation_run as run_lifecycle
from meho_backplane.operations import register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations.ingest import parse_openapi, register_ingested_operations
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
)

_PROFILES = {"fence-only": {"tools": "operate", "connectors": ["fence"], "read_only": False}}
_CONNECTOR_ID = "fence-1.0"
_INGESTED_OP = "GET:/items"
_METHOD_OP = "fence.items.method"
_MODULE_OP = "fence.items.module"

_SPEC = """
openapi: 3.0.3
info: {title: fence fixture, version: '1.0'}
paths:
  /items:
    get:
      operationId: items.list
      responses:
        '200': {description: ok}
"""

#: Every vendor call; a refused call leaves it empty.
_SENT: list[str] = []


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("MCP_PROFILE_CONNECTOR_LISTS", "true")
    monkeypatch.setenv("MCP_CONNECTOR_PROFILES", json.dumps(_PROFILES))
    get_settings.cache_clear()
    reset_flight_recorder_config_cache_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    _SENT.clear()
    yield
    reset_dispatcher_caches()
    clear_registry()
    reset_flight_recorder_config_cache_for_testing()
    get_settings.cache_clear()


class _BaseConnector(Connector):
    """Both connectors share the method handler, so the method fits either class."""

    accepts_ingested_operations: ClassVar[bool] = True

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> OperationResult:  # type: ignore[override]
        raise NotImplementedError

    async def _request_json(self, target: Any, method: str, path: str, **_: Any) -> Any:
        _SENT.append(f"{type(self).product} {method}:{path}")
        return []

    async def list_items(
        self, operator: Operator, target: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        _SENT.append(f"{type(self).product} method")
        return {"items": []}


class _FenceConnector(_BaseConnector):
    product = "fence"
    version = "1.0"
    impl_id = "fence"


class _OtherConnector(_BaseConnector):
    product = "other"
    version = "1.0"
    impl_id = "other"


async def _module_handler(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    _SENT.append("module")
    return {"items": []}


@pytest.fixture
def embedding() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


async def _seed(embedding: AsyncMock) -> uuid.UUID:
    """A tenant, a target per connector, and three ops that wait for approval."""
    register_connector_v2(product="fence", version="", impl_id="", cls=_FenceConnector)
    register_connector_v2(product="other", version="", impl_id="", cls=_OtherConnector)
    tenant_id = uuid.uuid4()
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="t"))
        for name, product in (("fence-target", "fence"), ("other-target", "other")):
            session.add(
                TargetORM(
                    tenant_id=tenant_id,
                    name=name,
                    aliases=[],
                    product=product,
                    host=f"{name}.example.test",
                    port=443,
                    secret_ref="targets/fixture",
                    auth_model="shared_service_account",
                )
            )
    for op_id, handler in ((_METHOD_OP, _BaseConnector.list_items), (_MODULE_OP, _module_handler)):
        await register_typed_operation(
            product="fence",
            version="1.0",
            impl_id="fence",
            op_id=op_id,
            handler=handler,
            summary="List items.",
            description="Lists the items.",
            parameter_schema={"type": "object"},
            safety_level="caution",
            requires_approval=True,
            when_to_use=None,
            embedding_service=embedding,
        )
    await register_ingested_operations(
        product="fence",
        version="1.0",
        impl_id="fence",
        spec_source="fence.yaml",
        operations=parse_openapi("file:///fence.yaml", content=_SPEC),
        embedding_service=embedding,
    )
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == _INGESTED_OP)
            )
        ).scalar_one()
        row.is_enabled = True
        row.safety_level, row.requires_approval = "caution", True
    return tenant_id


def _operator(tenant_id: uuid.UUID, profile: str | None = "fence-only") -> Operator:
    return Operator(
        sub="op-profile-target",
        raw_jwt="header.payload.signature",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
        mcp_tool_profile=profile,
    )


async def _approval_count() -> int:
    async with get_sessionmaker()() as session:
        return int(await session.scalar(select(func.count()).select_from(ApprovalRequest)) or 0)


def _args(op_id: str, target: str) -> dict[str, Any]:
    return {"connector_id": _CONNECTOR_ID, "op_id": op_id, "target": target, "params": {}}


_OPS = pytest.mark.parametrize("op_id", [_INGESTED_OP, _METHOD_OP, _MODULE_OP])


# ---------------------------------------------------------------------------
# The target of a call
# ---------------------------------------------------------------------------


@_OPS
@pytest.mark.asyncio
async def test_call_on_a_target_of_another_connector_is_refused(
    embedding: AsyncMock, op_id: str
) -> None:
    tenant_id = await _seed(embedding)

    result = await call_operation(_operator(tenant_id), _args(op_id, "other-target"))

    assert result["status"] == "denied", result
    assert "not in your 'fence-only' profile" in result["error"]
    assert _SENT == []
    assert await _approval_count() == 0


@_OPS
@pytest.mark.asyncio
async def test_call_on_a_target_of_the_profile_goes_on(embedding: AsyncMock, op_id: str) -> None:
    """The control: the same op on a target of the profile waits for approval as usual."""
    tenant_id = await _seed(embedding)

    result = await call_operation(_operator(tenant_id), _args(op_id, "fence-target"))

    assert result["status"] == "awaiting_approval", result
    assert await _approval_count() == 1


@_OPS
@pytest.mark.asyncio
async def test_without_a_profile_the_target_is_not_checked(
    embedding: AsyncMock, op_id: str
) -> None:
    tenant_id = await _seed(embedding)

    result = await call_operation(_operator(tenant_id, None), _args(op_id, "other-target"))

    assert result["status"] != "denied", result


@_OPS
@pytest.mark.asyncio
async def test_preview_on_a_target_of_another_connector_is_refused(
    embedding: AsyncMock, op_id: str
) -> None:
    tenant_id = await _seed(embedding)

    result = await preview_operation(_operator(tenant_id), _args(op_id, "other-target"))

    assert result["status"] == "error", result
    assert result["extras"]["error_code"] == "denied"
    assert "not in your 'fence-only' profile" in result["error"]
    assert "other-target.example.test" not in json.dumps(result)
    assert _SENT == []
    assert await _approval_count() == 0


@_OPS
@pytest.mark.asyncio
async def test_preview_on_a_target_of_the_profile_is_not_refused(
    embedding: AsyncMock, op_id: str
) -> None:
    tenant_id = await _seed(embedding)

    result = await preview_operation(_operator(tenant_id), _args(op_id, "fence-target"))

    assert result.get("extras", {}).get("error_code") != "denied", result
    assert _SENT == []


@pytest.mark.asyncio
async def test_a_target_without_a_connector_is_refused(embedding: AsyncMock) -> None:
    await _seed(embedding)
    target = type("T", (), {"product": "nothing", "preferred_impl_id": None, "tenant_id": None})()
    target.fingerprint = type("F", (), {"version": None})()  # type: ignore[attr-defined]

    assert await profile_target_denial("fence-only", target) is not None
    assert await profile_target_denial(None, target) is None
    assert await profile_target_denial("investigate", target) is None
    assert await profile_target_denial("fence-only", None) is None


# ---------------------------------------------------------------------------
# list_targets
# ---------------------------------------------------------------------------


async def _list_targets(operator: Operator, arguments: dict[str, Any]) -> list[str]:
    entry = get_tool("list_targets")
    assert entry is not None
    page = await entry[1](operator, arguments)
    return [target["name"] for target in page["targets"]]


@pytest.mark.parametrize("arguments", [{}, {"connector_id": _CONNECTOR_ID}])
@pytest.mark.asyncio
async def test_list_targets_shows_only_the_profile_targets(
    embedding: AsyncMock, arguments: dict[str, Any]
) -> None:
    tenant_id = await _seed(embedding)

    assert await _list_targets(_operator(tenant_id), arguments) == ["fence-target"]


@pytest.mark.asyncio
async def test_list_targets_without_a_profile_shows_every_target(embedding: AsyncMock) -> None:
    tenant_id = await _seed(embedding)

    names = await _list_targets(_operator(tenant_id, None), {})

    assert names == ["fence-target", "other-target"]


@pytest.mark.asyncio
async def test_list_targets_cursor_counts_the_rows_read(embedding: AsyncMock) -> None:
    """A page the profile made shorter still points to the next page."""
    tenant_id = await _seed(embedding)
    entry = get_tool("list_targets")
    assert entry is not None

    first = await entry[1](_operator(tenant_id), {"limit": 1})
    assert [t["name"] for t in first["targets"]] == ["fence-target"]
    assert first["next_cursor"] == "fence-target"
    second = await entry[1](_operator(tenant_id), {"limit": 1, "cursor": "fence-target"})
    assert second["targets"] == []
    assert second["next_cursor"] == "other-target"


# ---------------------------------------------------------------------------
# Background runs
# ---------------------------------------------------------------------------


async def _run(tenant_id: uuid.UUID, profile: str | None) -> uuid.UUID:
    async with get_sessionmaker()() as session, session.begin():
        session.add(Tenant(id=tenant_id, slug=f"t-{tenant_id.hex[:8]}", name="t"))
        await session.flush()
        row = await run_lifecycle.create_run(
            session,
            tenant_id=tenant_id,
            identity_sub="op-profile-target",
            origin=OperationRunOrigin.DIRECT,
            connector_id=_CONNECTOR_ID,
            op_id=_INGESTED_OP,
            mcp_profile=result_profile_scope(profile),
        )
        return row.id


async def _poll(operator: Operator, run_id: uuid.UUID) -> dict[str, Any]:
    arguments = {"connector_id": _CONNECTOR_ID, "op_id": _INGESTED_OP}
    return await poll_call(operator, arguments, str(run_id))


@pytest.mark.parametrize(
    ("started_under", "read_under", "readable"),
    [
        ("fence-only", "fence-only", True),
        ("fence-only", None, False),
        ("fence-only", "operate", False),
        (None, "fence-only", False),
        ("operate", "fence-only", False),
        (None, None, True),
        ("operate", None, True),
        (None, "investigate", True),
    ],
)
@pytest.mark.asyncio
async def test_a_run_is_read_only_under_the_profile_that_started_it(
    started_under: str | None, read_under: str | None, readable: bool
) -> None:
    tenant_id = uuid.uuid4()
    run_id = await _run(tenant_id, started_under)
    operator = _operator(tenant_id, read_under)

    if readable:
        assert (await _poll(operator, run_id))["status"] == "pending"
        return
    with pytest.raises(McpInvalidParamsError) as caught:
        await _poll(operator, run_id)
    assert caught.value.data["reason"] == "unknown_run"


# ---------------------------------------------------------------------------
# The write tool list
# ---------------------------------------------------------------------------

#: Tools of a built-in list that are not marked read-only but need not be in
#: the write tool list, each with why.
_NOT_MEHO_WRITES: dict[str, str] = {
    "call_operation": "the policy gate checks every operation, also the profile's rules",
}


def test_every_tool_that_may_write_is_in_the_write_tool_list() -> None:
    """A new tool that writes in a built-in list fails here until it is in ``MEHO_WRITE_TOOLS``."""
    missing = []
    for name in sorted(set().union(*TOOL_PROFILES.values())):
        entry = get_tool(name)
        assert entry is not None, name
        annotations = entry[0].annotations
        read_only = annotations is not None and annotations.readOnlyHint is True
        if not read_only and name not in MEHO_WRITE_TOOLS and name not in _NOT_MEHO_WRITES:
            missing.append(name)
    assert missing == [], (
        f"{missing} may change something: add them to MEHO_WRITE_TOOLS in mcp/profiles.py"
    )
