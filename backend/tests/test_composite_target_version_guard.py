# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The target version check of composite (multi-step) operations.

A connector states, with ``composite_supported_version_range``, the target
versions all composite ops of its catalog work on. ``dispatch`` and
``preview_dispatch`` refuse a composite with ``unqualified_target_version``
when the target's version is unknown, unreadable, or outside the range.
The steps of a composite are not checked again, so this one check at the
top is the only version check a composite gets.

The connector, the product and the targets here are made up.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
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
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, register_typed_operation, reset_dispatcher_caches
from meho_backplane.operations._composite_version_guard import composite_version_refusal
from meho_backplane.operations._request_preview import preview_dispatch
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")
_CONNECTOR_ID = "widget-api-9.0"
_COMPOSITE_OP = "widget.composite.restart_all"
_TYPED_OP = "widget.gadget.list"

#: The op ids each composite run reached, so a test can prove it did not run.
_RUNS: list[str] = []


class _WidgetConnector(Connector):
    """Made-up connector whose composites work on 9.x targets only."""

    product = "widget"
    version = "9.0"
    impl_id = "widget-api"
    composite_supported_version_range = ">=9.0,<10.0"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError


async def _widget_composite(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
    dispatch_child: Any,
) -> dict[str, Any]:
    """Composite handler: records that it ran, runs no step."""
    del operator, target, params, dispatch_child
    _RUNS.append(_COMPOSITE_OP)
    return {"restarted": 0}


async def _widget_typed(target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Typed handler of the same catalog."""
    del target, params
    _RUNS.append(_TYPED_OP)
    return {"gadgets": []}


class _Target:
    """The target fields the resolver and the version check read."""

    def __init__(self, *, fingerprint_version: str | None, version: str | None = None) -> None:
        self.id = uuid.uuid4()
        self.tenant_id = _TENANT
        self.name = "widget-target-a"
        self.host = "widget-a.example.invalid"
        self.port = 443
        self.auth_model = "shared_service_account"
        self.product = "widget"
        self.preferred_impl_id: str | None = None
        self.fingerprint = {"version": fingerprint_version} if fingerprint_version else None
        self.version = version


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
    register_connector_v2(
        product="widget", version="9.0", impl_id="widget-api", cls=_WidgetConnector
    )
    yield
    reset_dispatcher_caches()
    clear_registry()
    _RUNS.clear()


def _operator() -> Operator:
    return Operator(
        sub="op-widget",
        name="Widget Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _composite_row(*, requires_approval: bool = False) -> EndpointDescriptor:
    now = datetime.now(UTC)
    return EndpointDescriptor(
        id=uuid.uuid4(),
        tenant_id=None,
        product="widget",
        version="9.0",
        impl_id="widget-api",
        op_id=_COMPOSITE_OP,
        source_kind="composite",
        method=None,
        path=None,
        handler_ref=f"{__name__}._widget_composite",
        summary="Restart all gadgets.",
        description="Restart every gadget of the widget.",
        tags=[],
        parameter_schema={"type": "object"},
        response_schema=None,
        llm_instructions=None,
        safety_level="caution" if requires_approval else "safe",
        requires_approval=requires_approval,
        is_enabled=True,
        embedding=[0.1] * 384,
        custom_description=None,
        custom_notes=None,
        created_at=now,
        updated_at=now,
    )


async def _add_composite(*, requires_approval: bool = False) -> None:
    async with get_sessionmaker()() as s:
        s.add(_composite_row(requires_approval=requires_approval))
        await s.commit()


async def _audit_statuses(op_id: str) -> list[Any]:
    async with get_sessionmaker()() as s:
        rows = (await s.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
    return [row.payload.get("result_status") for row in rows]


@pytest.mark.asyncio
async def test_composite_runs_on_a_target_inside_the_range() -> None:
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version="9.0.2"),
        params={},
    )
    assert result.status == "ok", result.error
    assert _RUNS == [_COMPOSITE_OP]


@pytest.mark.asyncio
async def test_composite_is_refused_on_a_target_outside_the_range() -> None:
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version="8.0.3"),
        params={},
    )
    assert result.status == "error"
    assert result.extras["error_code"] == "unqualified_target_version"
    assert result.extras["target_product"] == "widget"
    assert result.extras["target_version"] == "8.0.3"
    assert ">=9.0,<10.0" in result.extras["reason"]
    assert "8.0.3" in result.extras["reason"]
    assert _RUNS == []
    assert await _audit_statuses(_COMPOSITE_OP) == ["unqualified_target_version"]


@pytest.mark.asyncio
async def test_operator_set_version_is_used_when_the_target_was_not_probed() -> None:
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version=None, version="8.0"),
        params={},
    )
    assert result.extras["error_code"] == "unqualified_target_version"
    assert result.extras["target_version"] == "8.0"
    assert _RUNS == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fingerprint_version", "reason_part"),
    [(None, "no known version"), ("9.0 build seven", "not a version number")],
)
async def test_composite_is_refused_when_the_target_version_cannot_be_checked(
    fingerprint_version: str | None, reason_part: str
) -> None:
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version=fingerprint_version),
        params={},
    )
    assert result.status == "error"
    assert result.extras["error_code"] == "unqualified_target_version"
    assert reason_part in result.extras["reason"]
    assert _RUNS == []
    assert await _audit_statuses(_COMPOSITE_OP) == ["unqualified_target_version"]


@pytest.mark.asyncio
async def test_composite_runs_unchecked_when_the_connector_states_no_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WidgetConnector, "composite_supported_version_range", None)
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version=None),
        params={},
    )
    assert result.status == "ok", result.error
    assert _RUNS == [_COMPOSITE_OP]


@pytest.mark.asyncio
async def test_composite_is_refused_when_the_stated_range_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_WidgetConnector, "composite_supported_version_range", "nine or later")
    await _add_composite()
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version="9.0.2"),
        params={},
    )
    assert result.extras["error_code"] == "unqualified_target_version"
    assert "cannot read" in result.extras["reason"]
    assert _RUNS == []


@pytest.mark.asyncio
async def test_preview_gives_the_same_refusal_without_an_audit_row() -> None:
    await _add_composite()
    target = _Target(fingerprint_version="8.0.3")
    preview = await preview_dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=target,
        params={},
    )
    assert preview["status"] == "error"
    assert preview["extras"]["error_code"] == "unqualified_target_version"
    assert preview["extras"]["target_version"] == "8.0.3"
    assert await _audit_statuses(_COMPOSITE_OP) == []

    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=target,
        params={},
    )
    assert result.extras["reason"] == preview["extras"]["reason"]


@pytest.mark.asyncio
async def test_approved_resume_is_checked_again() -> None:
    """A target that moved to another version while the call waited is refused."""
    await _add_composite(requires_approval=True)
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_COMPOSITE_OP,
        target=_Target(fingerprint_version="10.1"),
        params={},
        _approved=True,
    )
    assert result.extras["error_code"] == "unqualified_target_version"
    assert _RUNS == []


@pytest.mark.asyncio
async def test_typed_op_of_the_same_catalog_is_not_checked() -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    await register_typed_operation(
        product="widget",
        version="9.0",
        impl_id="widget-api",
        op_id=_TYPED_OP,
        handler=_widget_typed,
        summary="List gadgets.",
        description="List gadgets.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
    )
    result = await dispatch(
        operator=_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_TYPED_OP,
        target=_Target(fingerprint_version="8.0.3"),
        params={},
    )
    assert result.status == "ok", result.error
    assert _RUNS == [_TYPED_OP]


def test_check_skips_other_kinds_and_a_missing_target() -> None:
    composite = _composite_row()
    assert composite_version_refusal(composite, None) is None
    typed = _composite_row()
    typed.source_kind = "typed"
    assert composite_version_refusal(typed, _Target(fingerprint_version="8.0.3")) is None
    unregistered = _composite_row()
    unregistered.version = "1.0"
    assert composite_version_refusal(unregistered, _Target(fingerprint_version="8.0.3")) is None
